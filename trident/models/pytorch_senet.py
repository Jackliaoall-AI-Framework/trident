from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import inspect
import math
import os
import uuid
from collections import *
from collections import deque
from copy import copy, deepcopy
from functools import partial
from itertools import repeat

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch._six import container_abcs
from torch.nn import init
from torch.nn.parameter import Parameter

from trident.backend.common import *
from trident.backend.pytorch_backend import to_numpy, to_tensor, Layer, Sequential, fix_layer, load
from trident.data.image_common import *
from trident.data.utils import download_model_from_google_drive
from trident.layers.pytorch_activations import get_activation, Identity
from trident.layers.pytorch_blocks import *
from trident.layers.pytorch_layers import *
from trident.layers.pytorch_normalizations import get_normalization
from trident.layers.pytorch_pooling import *
from trident.optims.pytorch_trainer import *

__all__ = ['se_bottleneck', 'SE_ResNet','SE_ResNet50','SE_ResNet101','SE_ResNet152']

_session = get_session()
_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_epsilon=_session.epsilon
_trident_dir=_session.trident_dir


dirname = os.path.join(_trident_dir, 'models')
if not os.path.exists(dirname):
    try:
        os.makedirs(dirname)
    except OSError:
        # Except permission denied and potential race conditions
        # in multi-threaded environments.
        pass

model_urls = {
    'se_resnet50': '1vq0uueiHXuHSEFhb02GoEuPzLwrDW_Mb',
    'se_resnet101': '17moUOsGynsWALLHyv3yprHWbbDMrdiOP',
    'se_resnet152': '1BIaHb7_qunUVvt4TDAwonSKI2jYg4Ybj',
}

def basic_block(num_filters=64,base_width=64,strides=1,expansion  = 4,conv_shortcut=False,use_bias=False,name=''):
    shortcut = Identity()
    if strides>1 or conv_shortcut is True:
        shortcut =Conv2d_Block((1,1),num_filters=num_filters,strides=strides,auto_pad=True,padding_mode='zero',normalization='batch',activation=None,use_bias=use_bias,name=name + '_downsample')

    return ShortCut2d(Sequential(Conv2d_Block((3,3),num_filters=num_filters,strides=strides,auto_pad=True,padding_mode='zero',normalization='batch',activation='relu',use_bias=use_bias,name=name + '_0_conv'),
                                 Conv2d_Block((3,3),num_filters=num_filters,strides=1,auto_pad=True,padding_mode='zero',normalization='batch',activation=None,use_bias=use_bias,name=name + '_1_conv')),
                      shortcut,activation='relu')

def bottleneck(num_filters=64,strides=1,expansion = 4,conv_shortcut=True,use_bias=False,name=''):
    #width = int(num_filters * (base_width / 64.)) * 1#groups'
    shortcut = Identity()
    shortcut_name='Identity'
    if strides>1 or conv_shortcut is True:
        shortcut =Conv2d_Block((1,1),num_filters=num_filters*expansion,strides=strides,auto_pad=True,padding_mode='zero',normalization='batch',activation=None,use_bias=use_bias,name=name + '_downsample')
        shortcut_name = 'downsample'
    return ShortCut2d({'branch1':Sequential(Conv2d_Block((1,1),num_filters=num_filters ,strides=strides,auto_pad=True,padding_mode='zero',normalization='batch',activation='relu',use_bias=use_bias,name=name + '_0_conv'),
                                 Conv2d_Block((3, 3), num_filters=num_filters , strides=1, auto_pad=True,padding_mode='zero',normalization='batch', activation='relu',use_bias=use_bias,name=name + '_1_conv'),
                                 Conv2d_Block((1,1),num_filters=num_filters*expansion,strides=1,auto_pad=True,padding_mode='zero',normalization='batch',activation=None,use_bias=use_bias,name=name + '_2_conv')),
                      shortcut_name:shortcut},activation='relu')

def se_bottleneck(num_filters=64,strides=1,expansion = 4,conv_shortcut=True,use_bias=False,name=''):
    #width = int(num_filters * (base_width / 64.)) * 1#groups'
    shortcut = Identity()
    shortcut_name='Identity'
    if strides>1 or conv_shortcut is True:
        shortcut =Conv2d_Block((1,1),num_filters=num_filters*expansion,strides=strides,auto_pad=True,padding_mode='zero',normalization='batch',activation=None,use_bias=use_bias,name='downsample')
        shortcut_name = 'downsample'
    return ShortCut2d({'branch1':Sequential(
                                    Conv2d_Block((1,1),num_filters=num_filters ,strides=strides,auto_pad=True,padding_mode='zero',normalization='batch',activation='relu',use_bias=use_bias,name='conv1'),
                                 Conv2d_Block((3, 3), num_filters=num_filters , strides=1, auto_pad=True,padding_mode='zero',normalization='batch', activation='relu',use_bias=use_bias,name='conv2'),
                                 Conv2d_Block((1,1),num_filters=num_filters*expansion,strides=1,auto_pad=True,padding_mode='zero',normalization='batch',activation=None,use_bias=use_bias,name= 'conv3'),
                                SqueezeExcite(se_filters=num_filters//expansion,num_filters=num_filters*expansion,use_bias=True)),
                      shortcut_name:shortcut},activation='relu')

def SE_ResNet(block, layers, input_shape=(3, 224, 224), num_classes=1000, use_bias=False,  include_top=True, model_name='',
           **kwargs):
    """Instantiates the ResNet, ResNetV2, and ResNeXt architecture.

    Args
        block: a function that returns output tensor for the stacked residual blocks.
        layers: list of integer, the number of  repeat units in each blocks.
        input_shape: optional shape tuple, only to be specified
            if `include_top` is False (otherwise the input shape
            has to be`(3, 224, 224)` (with `channels_first` data format).
            It should have exactly 3 inputs channels.
        num_classes: optional number of classes to classify images
            into, only to be specified if `include_top` is True, and
            if no `weights` argument is specified.
        use_bias: whether to use biases for convolutional layers or not
            (True for ResNet and ResNetV2, False for ResNeXt).
        include_top: whether to include the fully-connected layer at the top of the network.
        model_name: string, model name.

    Returns
        A Keras model instance.

    Raises
        ValueError: in case of invalid argument for `weights`,  or invalid input shape.

    """


    def _make_layer(block, num_filters, blocklayers, strides=1, dilate=False,use_bias=use_bias,layer_name=''):
        conv_shortcut=False
        if strides!=1 or block is bottleneck or num_filters!=128:
            conv_shortcut=True

        layers = []
        layers.append(block(num_filters=num_filters, strides=strides, expansion = 4, conv_shortcut=conv_shortcut,use_bias=use_bias, name=layer_name+'.0'))

        for k in range(1, blocklayers):
            layers.append(block(num_filters=num_filters,  strides=1, expansion = 4, conv_shortcut=False, use_bias=use_bias,name=layer_name+'.{0}'.format(k)))

        layers_block=Sequential(*layers)
        layers_block.name=layer_name
        return layers_block

    flow_list=[]
    resnet = Sequential()
    layer0=Sequential(name='layer0')
    layer0.add_module('first_block',Conv2d_Block((7,7),64,strides=2,use_bias=use_bias,auto_pad=True,padding_mode='zero',normalization='batch',activation='relu',name='first_block'))
    layer0.add_module('maxpool',(MaxPool2d((3,3),strides=2,auto_pad=True,padding_mode='zero')))
    resnet.add_module('layer0', layer0)
    resnet.add_module('layer1',(_make_layer(block, 64, layers[0],strides=1, dilate=None,use_bias=use_bias,layer_name='layer1' )))
    resnet.add_module('layer2',(_make_layer(block, 128, layers[1], strides=2, dilate=None,use_bias=use_bias,layer_name='layer2' )))
    resnet.add_module('layer3',(_make_layer(block, 256, layers[2], strides=2, dilate=None,use_bias=use_bias,layer_name='layer3' )))
    resnet.add_module('layer4' ,(_make_layer(block, 512, layers[3], strides=2, dilate=None,use_bias=use_bias,layer_name='layer4' )))
    resnet.add_module('avg_pool',GlobalAvgPool2d(name='avg_pool'))
    if include_top:
        resnet.add_module('fc',Dense(num_classes,activation=None,name='fc'))
        resnet.add_module('softmax', SoftMax(name='softmax'))
    resnet.name=model_name
    model=ImageClassificationModel(input_shape=input_shape,output=resnet)


    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)) ,'imagenet_labels1.txt'), 'r', encoding='utf-8-sig') as f:
        labels = [l.rstrip() for l in f]
        model.class_names=labels
    model.preprocess_flow=[resize((input_shape[1],input_shape[2]),keep_aspect=True),normalize(0,255),normalize([0.485, 0.456, 0.406],[0.229, 0.224, 0.225])]
    #model.summary()
    return model

#

def SE_ResNet50(include_top=True,
             pretrained=True,
            freeze_features=False,
             input_shape=None,
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)
    else:
        input_shape=(3, 224, 224)
    resnet50 =SE_ResNet(se_bottleneck, [3, 4, 6, 3], input_shape,num_classes=classes,include_top=include_top, model_name='se_resnet50')
    if pretrained==True:
        download_model_from_google_drive(model_urls['se_resnet50'],dirname,'se_resnet50.pth')
        recovery_model=load(os.path.join(dirname,'se_resnet50.pth'))
        recovery_model = fix_layer(recovery_model)
        recovery_model.name = 'se_resnet50'
        recovery_model.eval()
        recovery_model.to(_device)
        if freeze_features:
            recovery_model.trainable = False
            recovery_model.fc.trainable = True
        if include_top==False:
            recovery_model.remove_at(-1)
            recovery_model.remove_at(-1)
            recovery_model.remove_at(-1)
            resnet50.class_names = []
        else:
            if classes!=1000:
                resnet50.class_names = []
                recovery_model.fc= Dense(classes, activation=None, name='fc')
                recovery_model.fc.input_shape=recovery_model.avg_pool.output_shape

        resnet50.model=recovery_model
    return resnet50

def SE_ResNet101(include_top=True,
             pretrained=True,
            freeze_features=False,
             input_shape=None,
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)
    else:
        input_shape=(3, 224, 224)
    resnet101 =SE_ResNet(se_bottleneck, [3, 4, 23, 3], input_shape,num_classes=classes,include_top=include_top, model_name='resnet101')
    if pretrained==True:
        download_model_from_google_drive(model_urls['se_resnet101'],dirname,'se_resnet101.pth')
        recovery_model=load(os.path.join(dirname,'se_resnet101.pth'))
        recovery_model = fix_layer(recovery_model)
        recovery_model.name = 'se_resnet101'
        recovery_model.eval()
        recovery_model.to(_device)
        if freeze_features:
            recovery_model.trainable = False
            recovery_model.fc.trainable = True
        if include_top == False:
            recovery_model.remove_at(-1)
            recovery_model.remove_at(-1)
            recovery_model.remove_at(-1)
            resnet101.class_names = []
        else:
            if classes != 1000:
                resnet101.class_names = []
                recovery_model.fc = Dense(classes, activation=None, name='fc')
                recovery_model.fc.input_shape=recovery_model.avg_pool.output_shape

        resnet101.model=recovery_model
    return resnet101


def SE_ResNet152(include_top=True,
             pretrained=True,
            freeze_features=False,
             input_shape=None,
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)
    else:
        input_shape=(3, 224, 224)
    resnet152 =SE_ResNet(se_bottleneck [3, 8, 36, 3], input_shape,num_classes=classes,include_top=include_top, model_name='resnet152')
    if pretrained==True:
        download_model_from_google_drive(model_urls['se_resnet152'],dirname,'se_resnet152.pth')
        recovery_model=load(os.path.join(dirname,'se_resnet152.pth'))
        recovery_model = fix_layer(recovery_model)
        recovery_model.name = 'se_resnet152'
        recovery_model.eval()
        recovery_model.to(_device)
        if freeze_features:
            recovery_model.trainable = False
            recovery_model.fc.trainable = True
        if include_top == False:
            recovery_model.remove_at(-1)
            recovery_model.remove_at(-1)
            recovery_model.remove_at(-1)
            resnet152.class_names = []
        else:
            if classes != 1000:
                resnet152.class_names=[]
                recovery_model.fc = Dense(classes, activation=None, name='fc')
                recovery_model.fc.input_shape=recovery_model.avg_pool.output_shape

        resnet152.model=recovery_model
    return resnet152


#
#
# resnet34=ResNet(basic_block, [3, 4, 6, 3], (3, 224, 224))
# resnet50=ResNet(bottleneck, [3, 4, 6, 3], (3, 224, 224))
# resnet101=ResNet(bottleneck, [3, 4, 23, 3], (3, 224, 224))
# resnet152=ResNet(bottleneck, [3, 8, 36, 3], (3, 224, 224))