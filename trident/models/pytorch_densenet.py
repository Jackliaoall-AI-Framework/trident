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
from trident.backend.pytorch_backend import to_numpy, to_tensor, Layer, Sequential, summary
from trident.data.image_common import *
from trident.data.utils import download_model_from_google_drive
from trident.layers.pytorch_activations import get_activation, Identity, Relu
from trident.layers.pytorch_blocks import *
from trident.layers.pytorch_layers import *
from trident.layers.pytorch_normalizations import get_normalization, BatchNorm2d
from trident.layers.pytorch_pooling import *
from trident.optims.pytorch_trainer import *

__all__ = ['DenseNet','DenseNet121','DenseNet161','DenseNet169','DenseNet201','DenseNetFcn']

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



def DenseLayer(growth_rate,name=''):
    items = OrderedDict()
    items['norm']=BatchNorm2d()
    items['relu']=Relu()
    items['conv1']=Conv2d_Block((1,1),4 * growth_rate,strides=1,activation='relu',auto_pad=True,padding_mode='zero',use_bias=False,normalization='batch')
    items['conv2']=Conv2d((3,3),growth_rate,strides=1,auto_pad=True,padding_mode='zero',use_bias=False)
    return  Sequential(items)


class DenseBlock(Layer):
    def __init__(self, num_layers,  growth_rate=32, drop_rate=0,keep_output=False,name=''):
        super(DenseBlock, self).__init__()
        if len(name)>0:
            self.name=name
        self.keep_output=keep_output
        for i in range(num_layers):
            layer = DenseLayer(growth_rate,name='denselayer%d' % (i + 1))
            self.add_module('denselayer%d' % (i + 1), layer)

    def forward(self, x):
        for name, layer in self.named_children():
            new_features = layer(x)
            x=torch.cat([x,new_features], 1)
        return x


def Transition(reduction,name=''):
    items=OrderedDict()
    items['norm']=BatchNorm2d()
    items['relu']=Relu()
    items['conv1']=Conv2d((1, 1),num_filters=None, depth_multiplier=reduction, strides=1, auto_pad=True,padding_mode='zero',use_bias=False)
    items['pool']=AvgPool2d(2,2,auto_pad=True)
    return Sequential(items,name=name)


def TransitionDown(reduction,name=''):
    return DepthwiseConv2d_Block((3,3),depth_multiplier=reduction,strides=2,activation='leaky_relu',normalization='batch', dropout_rate=0.2)

def TransitionUp(output_idx=None,num_filters=None,name=''):
    return ShortCut2d(TransConv2d((3,3),num_filters=num_filters,strides=2,auto_pad=True),output_idx=output_idx,mode= 'concate',name=name)

def DenseNet(blocks,
             growth_rate=32,
             initial_filters=64,
             include_top=True,
             pretrained=True,
             input_shape=(3,224,224),
             num_classes=1000,
             name='',
             **kwargs):
    '''Instantiates the DenseNet architecture.
        Optionally loads weights pre-trained on ImageNet.
        Note that the data format convention used by the model is
        the one specified in your Keras config at `~/.keras/keras.json`.
    Args
        blocks: numbers of building blocks for the four dense layers.
        include_top: whether to include the fully-connected
            layer at the top of the network.
        weights: one of `None` (random initialization),
              'imagenet' (pre-training on ImageNet),
              or the path to the weights file to be loaded.
        input_tensor: optional Keras tensor
            (i.e. output of `layers.Input()`)
            to use as image input for the model.
        input_shape: optional shape tuple, only to be specified
            if `include_top` is False (otherwise the input shape
            has to be `(224, 224, 3)` (with `'channels_last'` data format)
            or `(3, 224, 224)` (with `'channels_first'` data format).
            It should have exactly 3 inputs channels,
            and width and height should be no smaller than 32.
            E.g. `(200, 200, 3)` would be one valid value.
        pooling: optional pooling mode for feature extraction
            when `include_top` is `False`.
            - `None` means that the output of the model will be
                the 4D tensor output of the
                last convolutional block.
            - `avg` means that global average pooling
                will be applied to the output of the
                last convolutional block, and thus
                the output of the model will be a 2D tensor.
            - `max` means that global max pooling will
                be applied.
        classes: optional number of classes to classify images
            into, only to be specified if `include_top` is True, and
            if no `weights` argument is specified.
    Returns
        A Keras model instance.
    Raises
        ValueError: in case of invalid argument for `weights`,
            or invalid input shape.
    '''
    densenet=Sequential()
    densenet.add_module('conv1/conv',Conv2d_Block((7,7),initial_filters,strides=2,use_bias=False,auto_pad=True,padding_mode='zero',activation='relu',normalization='batch', name='conv1/conv'))
    densenet.add_module('maxpool', (MaxPool2d((3, 3), strides=2, auto_pad=True, padding_mode='zero')))
    densenet.add_module('denseblock1', DenseBlock(blocks[0],growth_rate=growth_rate))
    densenet.add_module('transitiondown1', Transition(0.5))
    densenet.add_module('denseblock2', DenseBlock(blocks[1], growth_rate=growth_rate))
    densenet.add_module('transitiondown2', Transition(0.5))
    densenet.add_module('denseblock3', DenseBlock(blocks[2], growth_rate=growth_rate))
    densenet.add_module('transitiondown3', Transition(0.5))
    densenet.add_module('denseblock4', DenseBlock(blocks[3], growth_rate=growth_rate))
    densenet.add_module('classifier_norm',BatchNorm2d(name='classifier_norm'))
    densenet.add_module('classifier_relu', Relu(name='classifier_relu'))
    densenet.add_module('avg_pool', GlobalAvgPool2d(name='avg_pool'))
    if include_top:
        densenet.add_module('classifier', Dense(num_classes, activation=None, name='classifier'))
        densenet.add_module('softmax', SoftMax( name='softmax'))
    densenet.name = name

    model=ImageClassificationModel(input_shape=input_shape,output=densenet)
    model.signature=get_signature(model.model.forward)
    #model.model.to(_device)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'imagenet_labels1.txt'), 'r',encoding='utf-8-sig') as f:
        labels = [l.rstrip() for l in f]
        model.class_names = labels
    model.preprocess_flow = [resize((input_shape[2], input_shape[1]), keep_aspect=True), normalize(0, 255),  normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])]
    # model.summary()
    return model


def DenseNetFcn(blocks=(4, 5, 7, 10, 12),
             growth_rate=16,
             initial_filters=64,
             pretrained=False,
             input_shape=(3,224,224),
             num_classes=10,
             name='',
             **kwargs):
    """Instantiates the DenseNet architecture.
    Optionally loads weights pre-trained on ImageNet.
    Note that the data format convention used by the model is
    the one specified in your Keras config at `~/.keras/keras.json`.
    # Arguments
        blocks: numbers of building blocks for the four dense layers.
        include_top: whether to include the fully-connected
            layer at the top of the network.
        weights: one of `None` (random initialization),
              'imagenet' (pre-training on ImageNet),
              or the path to the weights file to be loaded.
        input_tensor: optional Keras tensor
            (i.e. output of `layers.Input()`)
            to use as image input for the model.
        input_shape: optional shape tuple, only to be specified
            if `include_top` is False (otherwise the input shape
            has to be `(224, 224, 3)` (with `'channels_last'` data format)
            or `(3, 224, 224)` (with `'channels_first'` data format).
            It should have exactly 3 inputs channels,
            and width and height should be no smaller than 32.
            E.g. `(200, 200, 3)` would be one valid value.
        pooling: optional pooling mode for feature extraction
            when `include_top` is `False`.
            - `None` means that the output of the model will be
                the 4D tensor output of the
                last convolutional block.
            - `avg` means that global average pooling
                will be applied to the output of the
                last convolutional block, and thus
                the output of the model will be a 2D tensor.
            - `max` means that global max pooling will
                be applied.
        classes: optional number of classes to classify images
            into, only to be specified if `include_top` is True, and
            if no `weights` argument is specified.
    # Returns
        A Keras model instance.
    # Raises
        ValueError: in case of invalid argument for `weights`,
            or invalid input shape.
    """


    model = ImageSegmentationModel(input_shape=input_shape, output=_DenseNetFcn2(blocks=blocks,
             growth_rate=growth_rate,
             initial_filters=initial_filters,
             num_classes=num_classes,
             name=name,
             **kwargs))
    model.signature = get_signature(model.model.forward)

    model.preprocess_flow = [resize((input_shape[2], input_shape[1]), keep_aspect=True), normalize(0, 255),
                             normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])]
    # model.summary()
    return model

class _DenseNetFcn2(Layer):
    def __init__(self, blocks=(4, 5, 7, 10, 12),
             growth_rate=16,
             initial_filters=64,
             num_classes=10,
             name='',
             **kwargs):
        super(_DenseNetFcn2, self).__init__()
        self.blocks=blocks
        self.num_classes=num_classes
        self.growth_rate=growth_rate
        self.name=name
        self.initial_filters=initial_filters
        self.first_layer=Conv2d_Block((3, 3), num_filters=self.initial_filters, strides=2, use_bias=False, auto_pad=True,
                                                    padding_mode='zero', activation='relu', normalization='batch',
                                                    name='first_layer')
        for i in range(len(self.blocks)-1):
            num_filters=self.initial_filters+self.blocks[i+1]*self.growth_rate
            self.add_module('denseblock_down{0}'.format(i+1),DenseBlock(self.blocks[i], growth_rate=self.growth_rate, name='denseblock_down{0}'.format(i+1)))
            self.add_module('transition_down{0}'.format(i+1),TransitionDown(0.5,name='transition_down{0}'.format(i+1)))
            self.add_module('transition_up{0}'.format(i + 1), TransConv2d_Block((3,3),num_filters=num_filters,strides=2,auto_pad=True,activation='relu',normalization='batch',name='transition_up{0}'.format(i + 1)))
            self.add_module('denseblock_up{0}'.format(i + 1),DenseBlock(self.blocks[i], growth_rate=self.growth_rate, name='denseblock_up{0}'.format(i + 1)))

        self.bottleneck=DenseBlock(self.blocks[4], growth_rate=self.growth_rate, name='bottleneck')
        self.upsample= Upsampling2d(scale_factor=2,mode='bilinear')
        self.last_layer=Conv2d((1, 1), num_filters=self.num_classes, strides=1, activation=None)
        self.softmax=SoftMax()

    def forward(self, *x):
        x=enforce_singleton(x)
        skips=[]
        x=self.first_layer(x)
        for i in range(len(self.blocks) - 1):
            x=getattr(self,'denseblock_down{0}'.format(i+1))(x)
            skips.append(x)
            x=getattr(self,'transition_down{0}'.format(i+1))(x)

        x=self.bottleneck(x)
        for i in range(len(self.blocks) - 1):
            x = getattr(self, 'transition_up{0}'.format(len(self.blocks)-1- i))(x)
            output = skips.pop()
            x = torch.cat([x, output], dim=1)
            x=getattr(self,'denseblock_up{0}'.format(len(self.blocks)-1-i))(x)
        x=self.upsample(x)
        x=self.last_layer(x)
        x=self.softmax(x)
        return x



def DenseNet121(include_top=True,
             pretrained=True,
             input_shape=(3,224,224),
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)

    densenet121 =DenseNet([6, 12, 24, 16],32,64, include_top=include_top, pretrained=True,input_shape=input_shape, num_classes=classes,name='densenet121')
    if pretrained==True:
        download_model_from_google_drive('16N2BECErDMRTV5JqESEBWyylXbQmKAIk',dirname,'densenet121.pth')
        recovery_model=torch.load(os.path.join(dirname,'densenet121.pth'))
        recovery_model.eval()
        recovery_model.to(_device)
        if include_top==False:
            recovery_model.__delitem__(-1)
        else:
            if classes!=1000:
                new_fc = Dense(classes, activation=None, name='classifier')
                new_fc.input_shape=recovery_model.classifier.input_shape
                recovery_model.classifier=new_fc
        densenet121.model=recovery_model
        densenet121.rebinding_input_output(input_shape)
        densenet121.signature = get_signature(densenet121.model.forward)
    return densenet121


def DenseNet161(include_top=True,
             pretrained=True,
             input_shape=(3,224,224),
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)

    densenet161 =DenseNet([6, 12, 36, 24],48,96, include_top=include_top, pretrained=True,input_shape=input_shape, num_classes=classes,name='densenet161')
    if pretrained==True:
        download_model_from_google_drive('1n3HRkdPbxKrLVua9gOCY6iJnzM8JnBau',dirname,'densenet161.pth')
        recovery_model=torch.load(os.path.join(dirname,'densenet161.pth'))
        recovery_model.eval()
        recovery_model.to(_device)
        if include_top==False:
            recovery_model.__delitem__(-1)
        else:
            if classes!=1000:
                new_fc = Dense(classes, activation=None, name='classifier')
                new_fc.input_shape=recovery_model.classifier.input_shape
                recovery_model.classifier=new_fc
        densenet161.model=recovery_model
        densenet161.rebinding_input_output(input_shape)
        densenet161.signature = get_signature(densenet161.model.forward)
    return densenet161




def DenseNet169(include_top=True,
             pretrained=True,
             input_shape=(3,224,224),
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)

    densenet169 =DenseNet([6, 12, 32, 32],32,64, include_top=include_top, pretrained=True,input_shape=input_shape, num_classes=classes,name='densenet169')
    if pretrained==True:
        download_model_from_google_drive('1QV73Th0Wo4SCq9AFPVEKqnzs7BUvIG5B',dirname,'densenet169.pth')
        recovery_model=torch.load(os.path.join(dirname,'densenet169.pth'))
        recovery_model.eval()
        recovery_model.to(_device)
        if include_top==False:
            recovery_model.__delitem__(-1)
        else:
            if classes!=1000:
                new_fc = Dense(classes, activation=None, name='classifier')
                new_fc.input_shape=recovery_model.classifier.input_shape
                recovery_model.classifier=new_fc
        densenet169.model=recovery_model
        densenet169.rebinding_input_output(input_shape)
        densenet169.signature = get_signature(densenet169.model.forward)
    return densenet169



def DenseNet201(include_top=True,
             pretrained=True,
             input_shape=(3,224,224),
             classes=1000,
             **kwargs):
    if input_shape is not None and len(input_shape)==3:
        input_shape=tuple(input_shape)

    densenet201 =DenseNet([6, 12, 48, 32],32,64, include_top=include_top, pretrained=True,input_shape=input_shape, num_classes=classes,name='densenet201')
    if pretrained==True:
        download_model_from_google_drive('1V2JazzdnrU64lDfE-O4bVIgFNQJ38q3J',dirname,'densenet201.pth')
        recovery_model=torch.load(os.path.join(dirname,'densenet201.pth'))
        recovery_model.eval()
        recovery_model.to(_device)
        if include_top==False:
            recovery_model.__delitem__(-1)
        else:
            if classes!=1000:
                new_fc = Dense(classes, activation=None, name='classifier')
                new_fc.input_shape=recovery_model.classifier.input_shape
                recovery_model.classifier=new_fc
        densenet201.model=recovery_model
        densenet201.rebinding_input_output(input_shape)
        densenet201.signature = get_signature(densenet201.model.forward)
    return densenet201