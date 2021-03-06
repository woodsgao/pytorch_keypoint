import json
import os
import os.path as osp
import random

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from imgaug import augmenters as ia
from imgaug.augmentables.polys import Polygon, PolygonsOnImage

from pytorch_modules.utils import IMG_EXT

TRAIN_AUGS = ia.SomeOf(
    [0, 3],
    [
        # ia.WithColorspace(
        #     to_colorspace='HSV',
        #     from_colorspace='RGB',
        #     children=ia.Sequential([
        #         ia.WithChannels(
        #             0,
        #             ia.SomeOf([0, None],
        #                       [ia.Add((-10, 10)),
        #                        ia.Multiply((0.95, 1.05))],
        #                       random_state=True)),
        #         ia.WithChannels(
        #             1,
        #             ia.SomeOf([0, None],
        #                       [ia.Add((-50, 50)),
        #                        ia.Multiply((0.8, 1.2))],
        #                       random_state=True)),
        #         ia.WithChannels(
        #             2,
        #             ia.SomeOf([0, None],
        #                       [ia.Add((-50, 50)),
        #                        ia.Multiply((0.8, 1.2))],
        #                       random_state=True)),
        #     ])),
        ia.Dropout([0.015, 0.1]),  # drop 5% or 20% of all pixels
        # ia.Sharpen((0.0, 1.0)),  # sharpen the image
        ia.Affine(
            scale=(0.8, 1.2),
            translate_percent=(-0.1, 0.1),
            rotate=(-45, 45),
            shear=(-0.1,
                   0.1)),  # rotate by -45 to 45 degrees (affects heatmaps)
        # ia.ElasticTransformation(
        #     alpha=(0, 10),
        #     sigma=(0, 10)),  # apply water effect (affects heatmaps)
        # ia.PiecewiseAffine(scale=(0, 0.03), nb_rows=(2, 6), nb_cols=(2, 6)),
        # ia.GaussianBlur((0, 3)),
        ia.Fliplr(0.1),
        ia.Flipud(0.1),
        # ia.LinearContrast((0.5, 1)),
        # ia.AdditiveGaussianNoise(loc=(0, 10), scale=(0, 10))
    ],
    random_state=True)


class BasicDataset(torch.utils.data.Dataset):
    def __init__(self, img_size, augments, multi_scale, rect):
        super(BasicDataset, self).__init__()
        self.img_size = img_size
        self.rect = rect
        self.multi_scale = multi_scale
        self.augments = augments
        self.data = []

    def get_data(self, idx):
        return None, None

    def __getitem__(self, idx):
        img, kps = self.get_data(idx)
        img = img[..., ::-1]
        h, w, c = img.shape

        if self.rect:
            scale = min(self.img_size[0] / w, self.img_size[1] / h)
            resize = ia.Sequential([
                ia.Resize({
                    'width': int(w * scale),
                    'height': int(h * scale)
                }),
                ia.PadToFixedSize(*self.img_size,
                                  pad_cval=[123.675, 116.28, 103.53],
                                  position='center')
            ])
        else:
            resize = ia.Resize({
                'width': self.img_size[0],
                'height': self.img_size[1]
            })

        img = resize.augment_image(img)
        kps = resize.augment_polygons(kps)
        # augment
        if self.augments is not None:
            augments = self.augments.to_deterministic()
            img = augments.augment_image(img)
            kps = augments.augment_polygons(kps)
        heats = [np.zeros(img.shape[:2])] * len(self.classes)
        for kp in kps.polygons:
            c = kp.label
            point = kp.exterior.astype(np.int32)
            x = np.arange(img.shape[1], dtype=np.float)
            y = np.arange(img.shape[0], dtype=np.float)
            xx, yy = np.meshgrid(x, y)

            # evaluate kernels at grid points
            xxyy = np.c_[xx.ravel(), yy.ravel()]
            sigma = 10  # 65.9  # math.sqrt(- math.pow(100, 2) / math.log(0.1))
            xxyy -= point
            x_term = xxyy[:, 0]**2
            y_term = xxyy[:, 1]**2
            exp_value = -(x_term + y_term) / 2 / pow(sigma, 2)
            zz = np.exp(exp_value)
            heat = zz.reshape(img.shape[:2])
            heats[c] = heat
            # cv2.imshow('c', (heat * 255).astype(np.uint8))
            # cv2.waitKey(0)

        heats = np.stack(heats, 0)

        img = img.transpose(2, 0, 1)
        img = np.ascontiguousarray(img)

        return torch.ByteTensor(img), torch.FloatTensor(heats)

    def __len__(self):
        return len(self.data)

    def post_fetch_fn(self, batch):
        imgs, heats = batch
        imgs = imgs.float()
        imgs -= torch.FloatTensor([123.675, 116.28,
                                   103.53]).reshape(1, 3, 1, 1).to(imgs.device)
        imgs /= torch.FloatTensor([58.395, 57.12,
                                   57.375]).reshape(1, 3, 1, 1).to(imgs.device)
        if self.multi_scale:
            h = imgs.size(2)
            w = imgs.size(3)
            scale = random.uniform(0.7, 1.5)
            h = int(h * scale / 32) * 32
            w = int(w * scale / 32) * 32
            imgs = F.interpolate(imgs, (h, w))
        return (imgs, heats)


class CocoDataset(BasicDataset):
    def __init__(self,
                 path,
                 img_size=224,
                 augments=TRAIN_AUGS,
                 multi_scale=False,
                 rect=False):
        super(CocoDataset, self).__init__(img_size=img_size,
                                          augments=augments,
                                          multi_scale=multi_scale,
                                          rect=rect)
        with open(path, 'r') as f:
            self.coco = json.loads(f.read())
        self.img_root = osp.dirname(path)
        self.augments = augments
        self.classes = []
        self.build_data()
        self.data.sort()

    def build_data(self):
        img_ids = []
        img_paths = []
        img_anns = []
        self.classes = [c['name'] for c in self.coco['categories']]
        for img_info in self.coco['images']:
            img_ids.append(img_info['id'])
            img_paths.append(osp.join(self.img_root, img_info['file_name']))
            img_anns.append([])
        for ann in self.coco['annotations']:
            idx = ann['image_id']
            idx = img_ids.index(idx)
            img_anns[idx].append(ann)
        self.data = list(zip(img_paths, img_anns))

    def get_data(self, idx):
        img = cv2.imread(self.data[idx][0])
        polygons = []
        anns = self.data[idx][1]
        polygons = []
        for ann in anns:
            polygons.append(
                Polygon(
                    np.float32(ann['bbox'][:2]).reshape(-1, 2),
                    ann['category_id']))
        polygons = PolygonsOnImage(polygons, img.shape)
        return img, polygons
