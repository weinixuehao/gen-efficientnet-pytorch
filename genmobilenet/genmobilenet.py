""" Generic MobileNet

A generic MobileNet class with building blocks to support a variety of models:
* MNasNet B1, A1 (SE), Small
* MobileNetV2
* FBNet-C (TODO A & B)
* ChamNet (TODO still guessing at architecture definition)
* Single-Path NAS Pixel1
* ShuffleNetV2 (TODO add IR shuffle block)
* And likely more...

TODO not all combinations and variations have been tested. Currently working on training hyper-params...

Hacked together by Ross Wightman
"""

import math

import torch.nn as nn
import torch.nn.functional as F

from genmobilenet.helpers import load_state_dict_from_url
from genmobilenet.mobilenet_builder import *

__all__ = ['GenMobileNet', 'mnasnet_050', 'mnasnet_075', 'mnasnet_100', 'mnasnet_140',
           'semnasnet_050', 'semnasnet_075', 'semnasnet_100', 'semnasnet_140', 'mnasnet_small',
           'mobilenetv1_100', 'mobilenetv2_100', 'chamnetv1_100', 'chamnetv2_100',
           'fbnetc_100', 'spnasnet_100']


model_urls = {
    'mnasnet_050': None,
    'mnasnet_075': None,
    'mnasnet_100': None,
    'tflite_mnasnet_100': 'https://www.dropbox.com/s/q55ir3tx8mpeyol/tflite_mnasnet_100-31639cdc.pth?dl=1',
    'mnasnet_140': None,
    'semnasnet_050': None,
    'semnasnet_075': None,
    'semnasnet_100': None,
    'tflite_semnasnet_100':  'https://www.dropbox.com/s/yiori47sr9dydev/tflite_semnasnet_100-7c780429.pth?dl=1',
    'semnasnet_140': None,
    'mnasnet_small': None,
    'mobilenetv1_100': None,
    'mobilenetv2_100': None,
    'mobilenetv3_050': None,
    'mobilenetv3_075': None,
    'mobilenetv3_100': None,
    'chamnetv1_100': None,
    'chamnetv2_100': None,
    'fbnetc_100': None,
    'spnasnet_100': 'https://www.dropbox.com/s/iieopt18rytkgaa/spnasnet_100-048bc3f4.pth?dl=1',
}


class GenMobileNet(nn.Module):
    """ Generic Mobile Net

    An implementation of mobile optimized networks that covers:
      * MobileNet V1, V2, and V3
      * MNASNet A1, B1, and small
      * FBNet C (TBD A and B)
      * ChamNet (arch details are murky)
      * Single-Path NAS Pixel1
    """

    def __init__(self, block_args, num_classes=1000, in_chans=3, stem_size=32, num_features=1280,
                 depth_multiplier=1.0, depth_divisor=8, min_depth=None,
                 bn_momentum=BN_MOMENTUM_DEFAULT, bn_eps=BN_EPS_DEFAULT,
                 drop_rate=0., act_fn=F.relu, se_gate_fn=torch.sigmoid, se_reduce_mid=False,
                 head_conv='default', weight_init='goog', folded_bn=False, padding_same=False):
        super(GenMobileNet, self).__init__()
        self.drop_rate = drop_rate
        self.act_fn = act_fn

        stem_size = round_channels(stem_size, depth_multiplier, depth_divisor, min_depth)
        self.conv_stem = sconv2d(
            in_chans, stem_size, 3,
            padding=padding_arg(1, padding_same), stride=2, bias=folded_bn)
        self.bn1 = None if folded_bn else nn.BatchNorm2d(stem_size, momentum=bn_momentum, eps=bn_eps)
        in_chs = stem_size

        builder = MobilenetBuilder(
            depth_multiplier, depth_divisor, min_depth,
            act_fn, se_gate_fn, se_reduce_mid,
            bn_momentum, bn_eps, folded_bn, padding_same)
        self.blocks = nn.Sequential(*builder(in_chs, block_args))
        in_chs = builder.in_chs

        if not head_conv or head_conv == 'none':
            self.efficient_head = False
            self.conv_head = None
            assert in_chs == num_features
        else:
            self.efficient_head = head_conv == 'efficient'
            self.conv_head = sconv2d(
                in_chs, num_features, 1,
                padding=padding_arg(0, padding_same), bias=folded_bn and not self.efficient_head)
            self.bn2 = None if (folded_bn or self.efficient_head) else \
                nn.BatchNorm2d(num_features, momentum=bn_momentum, eps=bn_eps)

        self.classifier = nn.Linear(num_features, num_classes)

        for m in self.modules():
            if weight_init == 'goog':
                _initialize_weight_goog(m)
            else:
                _initialize_weight_default(m)

    def features(self, x):
        x = self.conv_stem(x)
        if self.bn1 is not None:
            x = self.bn1(x)
        x = self.act_fn(x)
        x = self.blocks(x)
        if self.efficient_head:
            x = F.adaptive_avg_pool2d(x, 1)
            x = self.conv_head(x)
            # no BN
            x = self.act_fn(x)
        else:
            if self.conv_head is not None:
                x = self.conv_head(x)
                if self.bn2 is not None:
                    x = self.bn2(x)
                x = self.act_fn(x)
            x = F.adaptive_avg_pool2d(x, 1)
        return x

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        if self.drop_rate > 0.:
            x = F.dropout(x, p=self.drop_rate, training=self.training)
        return self.classifier(x)


# Defaults used for Google/Tensorflow training of mobile networks /w RMSprop as per
# papers and TF reference implementations. PT momentum equiv for TF decay is (1 - TF decay)
# NOTE: momentum varies btw .99 and .9997 depending on source
# .99 in official TF TPU impl
# .9997 (/w .999 in search space) for paper
_BN_MOMENTUM_TF_DEFAULT = 1 - 0.99
_BN_EPS_TF_DEFAULT = 1e-3


def _resolve_bn_params(kwargs):
    # NOTE kwargs passed as dict intentionally
    bn_momentum_default = BN_MOMENTUM_DEFAULT
    bn_eps_default = BN_EPS_DEFAULT
    bn_tf = kwargs.pop('bn_tf', False)
    if bn_tf:
        bn_momentum_default = _BN_MOMENTUM_TF_DEFAULT
        bn_eps_default = _BN_EPS_TF_DEFAULT
    bn_momentum = kwargs.pop('bn_momentum', None)
    bn_eps = kwargs.pop('bn_eps', None)
    if bn_momentum is None:
        bn_momentum = bn_momentum_default
    if bn_eps is None:
        bn_eps = bn_eps_default
    return bn_momentum, bn_eps


def _initialize_weight_goog(m):
    # weight init as per Tensorflow Official impl
    # https://github.com/tensorflow/tpu/blob/master/models/official/mnasnet/mnasnet_model.py
    if isinstance(m, nn.Conv2d):
        n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels  # fan-out
        m.weight.data.normal_(0, math.sqrt(2.0 / n))
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.BatchNorm2d):
        m.weight.data.fill_(1.0)
        m.bias.data.zero_()
    elif isinstance(m, nn.Linear):
        n = m.weight.size(0)  # fan-out
        init_range = 1.0 / math.sqrt(n)
        m.weight.data.uniform_(-init_range, init_range)
        m.bias.data.zero_()


def _initialize_weight_default(m):
    if isinstance(m, nn.Conv2d):
        nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
    elif isinstance(m, nn.BatchNorm2d):
        m.weight.data.fill_(1.0)
        m.bias.data.zero_()
    elif isinstance(m, nn.Linear):
        nn.init.kaiming_uniform_(m.weight, mode='fan_in', nonlinearity='linear')


def _gen_mnasnet_a1(depth_multiplier, num_classes=1000, **kwargs):
    """Creates a mnasnet-a1 model.

    Ref impl: https://github.com/tensorflow/tpu/tree/master/models/official/mnasnet
    Paper: https://arxiv.org/pdf/1807.11626.pdf.

    Args:
      depth_multiplier: multiplier to number of channels per layer.
    """
    arch_def = [
        # stage 0, 112x112 in
        ['ds_r1_k3_s1_e1_c16_noskip'],
        # stage 1, 112x112 in
        ['ir_r2_k3_s2_e6_c24'],
        # stage 2, 56x56 in
        ['ir_r3_k5_s2_e3_c40_se0.25'],
        # stage 3, 28x28 in
        ['ir_r4_k3_s2_e6_c80'],
        # stage 4, 14x14in
        ['ir_r2_k3_s1_e6_c112_se0.25'],
        # stage 5, 14x14in
        ['ir_r3_k5_s2_e6_c160_se0.25'],
        # stage 6, 7x7 in
        ['ir_r1_k3_s1_e6_c320'],
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def _gen_mnasnet_b1(depth_multiplier, num_classes=1000, **kwargs):
    """Creates a mnasnet-b1 model.

    Ref impl: https://github.com/tensorflow/tpu/tree/master/models/official/mnasnet
    Paper: https://arxiv.org/pdf/1807.11626.pdf.

    Args:
      depth_multiplier: multiplier to number of channels per layer.
    """
    arch_def = [
        # stage 0, 112x112 in
        ['ds_r1_k3_s1_c16_noskip'],
        # stage 1, 112x112 in
        ['ir_r3_k3_s2_e3_c24'],
        # stage 2, 56x56 in
        ['ir_r3_k5_s2_e3_c40'],
        # stage 3, 28x28 in
        ['ir_r3_k5_s2_e6_c80'],
        # stage 4, 14x14in
        ['ir_r2_k3_s1_e6_c96'],
        # stage 5, 14x14in
        ['ir_r4_k5_s2_e6_c192'],
        # stage 6, 7x7 in
        ['ir_r1_k3_s1_e6_c320_noskip']
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def _gen_mnasnet_small(depth_multiplier, num_classes=1000, **kwargs):
    """Creates a mnasnet-b1 model.

    Ref impl: https://github.com/tensorflow/tpu/tree/master/models/official/mnasnet
    Paper: https://arxiv.org/pdf/1807.11626.pdf.

    Args:
      depth_multiplier: multiplier to number of channels per layer.
    """
    arch_def = [
        ['ds_r1_k3_s1_c8'],
        ['ir_r1_k3_s2_e3_c16'],
        ['ir_r2_k3_s2_e6_c16'],
        ['ir_r4_k5_s2_e6_c32_se0.25'],
        ['ir_r3_k3_s1_e6_c32_se0.25'],
        ['ir_r3_k5_s2_e6_c88_se0.25'],
        ['ir_r1_k3_s1_e6_c144']
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=8,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def _gen_mobilenet_v1(depth_multiplier, num_classes=1000, **kwargs):
    """ Generate MobileNet-V1 network
    Ref impl: https://github.com/tensorflow/models/blob/master/research/slim/nets/mobilenet/mobilenet_v2.py
    Paper: https://arxiv.org/abs/1801.04381
    """
    arch_def = [
        ['dsa_r1_k3_s1_c64'],
        ['dsa_r2_k3_s2_c128'],
        ['dsa_r2_k3_s2_c256'],
        ['dsa_r6_k3_s2_c512'],
        ['dsa_r2_k3_s2_c1024'],
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        num_features=1024,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        act_fn=F.relu6,
        head_conv='none',
        **kwargs
        )
    return model


def _gen_mobilenet_v2(depth_multiplier, num_classes=1000, **kwargs):
    """ Generate MobileNet-V2 network
    Ref impl: https://github.com/tensorflow/models/blob/master/research/slim/nets/mobilenet/mobilenet_v2.py
    Paper: https://arxiv.org/abs/1801.04381
    """
    arch_def = [
        ['ds_r1_k3_s1_c16'],
        ['ir_r2_k3_s2_e6_c24'],
        ['ir_r3_k3_s2_e6_c32'],
        ['ir_r4_k3_s2_e6_c64'],
        ['ir_r3_k3_s1_e6_c96'],
        ['ir_r3_k3_s2_e6_c160'],
        ['ir_r1_k3_s1_e6_c320'],
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        act_fn=F.relu6,
        **kwargs
    )
    return model


def _gen_mobilenet_v3(depth_multiplier, num_classes=1000, **kwargs):
    """Creates a MobileNet-V3 model.

    Ref impl: ?
    Paper: https://arxiv.org/abs/1905.02244

    Args:
      depth_multiplier: multiplier to number of channels per layer.
    """
    arch_def = [
        # stage 0, 112x112 in
        ['ds_r1_k3_s1_e1_c16_are_noskip'],  # relu
        # stage 1, 112x112 in
        ['ir_r1_k3_s2_e4_c24_are', 'ir_r1_k3_s1_e3_c24_are'],  # relu
        # stage 2, 56x56 in
        ['ir_r3_k5_s2_e3_c40_se0.25_are'],  # relu
        # stage 3, 28x28 in
        # FIXME are expansions here correct?
        ['ir_r1_k3_s2_e6_c80', 'ir_r1_k3_s1_e2.5_c80', 'ir_r2_k3_s1_e2.3_c80'],  # hard-swish
        # stage 4, 14x14in
        ['ir_r2_k3_s1_e6_c112_se0.25'],  # hard-swish
        # stage 5, 14x14in
        # FIXME paper has a mistaken block-stride pattern 1-2-1 that doesn't fit the usual 2-1-..., ignoring
        # The paper numbers result in an exp factor of 4.2 in the middle of this block, but keeping at 6
        # results in a param count closer to 5.4m
        ['ir_r1_k5_s2_e6_c160_se0.25', 'ir_r1_k5_s1_e6_c160_se0.25', 'ir_r1_k5_s1_e6_c160_se0.25'],  # hard-swish
        # stage 6, 7x7 in
        ['cn_r1_k1_s1_c960'],  # hard-swish
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=16,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        act_fn=hard_swish,
        se_gate_fn=hard_sigmoid,
        se_reduce_mid=True,
        head_conv='efficient',
        **kwargs
    )
    return model


def _gen_chamnet_v1(depth_multiplier, num_classes=1000, **kwargs):
    """ Generate Chameleon Network (ChamNet)

    Paper: https://arxiv.org/abs/1812.08934
    Ref Impl: https://github.com/facebookresearch/maskrcnn-benchmark/blob/master/maskrcnn_benchmark/modeling/backbone/fbnet_modeldef.py

    FIXME: this a bit of an educated guess based on trunkd def in maskrcnn_benchmark
    """
    arch_def = [
        ['ir_r1_k3_s1_e1_c24'],
        ['ir_r2_k7_s2_e4_c48'],
        ['ir_r5_k3_s2_e7_c64'],
        ['ir_r7_k5_s2_e12_c56'],
        ['ir_r5_k3_s1_e8_c88'],
        ['ir_r4_k3_s2_e7_c152'],
        ['ir_r1_k3_s1_e10_c104'],
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        num_features=1280,  # no idea what this is? try mobile/mnasnet default?
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def _gen_chamnet_v2(depth_multiplier, num_classes=1000, **kwargs):
    """ Generate Chameleon Network (ChamNet)

    Paper: https://arxiv.org/abs/1812.08934
    Ref Impl: https://github.com/facebookresearch/maskrcnn-benchmark/blob/master/maskrcnn_benchmark/modeling/backbone/fbnet_modeldef.py

    FIXME: this a bit of an educated guess based on trunk def in maskrcnn_benchmark
    """
    arch_def = [
        ['ir_r1_k3_s1_e1_c24'],
        ['ir_r4_k5_s2_e8_c32'],
        ['ir_r6_k7_s2_e5_c48'],
        ['ir_r3_k5_s2_e9_c56'],
        ['ir_r6_k3_s1_e6_c56'],
        ['ir_r6_k3_s2_e2_c152'],
        ['ir_r1_k3_s1_e6_c112'],
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        num_features=1280,  # no idea what this is? try mobile/mnasnet default?
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def _gen_fbnetc(depth_multiplier, num_classes=1000, **kwargs):
    """ FBNet-C

        Paper: https://arxiv.org/abs/1812.03443
        Ref Impl: https://github.com/facebookresearch/maskrcnn-benchmark/blob/master/maskrcnn_benchmark/modeling/backbone/fbnet_modeldef.py

        NOTE: the impl above does not relate to the 'C' variant here, that was derived from paper,
        it was used to confirm some building block details
    """
    arch_def = [
        ['ir_r1_k3_s1_e1_c16'],
        ['ir_r1_k3_s2_e6_c24', 'ir_r2_k3_s1_e1_c24'],
        ['ir_r1_k5_s2_e6_c32', 'ir_r1_k5_s1_e3_c32', 'ir_r1_k5_s1_e6_c32', 'ir_r1_k3_s1_e6_c32'],
        ['ir_r1_k5_s2_e6_c64', 'ir_r1_k5_s1_e3_c64', 'ir_r2_k5_s1_e6_c64'],
        ['ir_r3_k5_s1_e6_c112', 'ir_r1_k5_s1_e3_c112'],
        ['ir_r4_k5_s2_e6_c184'],
        ['ir_r1_k3_s1_e6_c352'],
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=16,
        num_features=1984,  # paper suggests this, but is not 100% clear
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def _gen_spnasnet(depth_multiplier, num_classes=1000, **kwargs):
    """Creates the Single-Path NAS model from search targeted for Pixel1 phone.

    Paper: https://arxiv.org/abs/1904.02877

    Args:
      depth_multiplier: multiplier to number of channels per layer.
    """
    arch_def = [
        # stage 0, 112x112 in
        ['ds_r1_k3_s1_c16_noskip'],
        # stage 1, 112x112 in
        ['ir_r3_k3_s2_e3_c24'],
        # stage 2, 56x56 in
        ['ir_r1_k5_s2_e6_c40', 'ir_r3_k3_s1_e3_c40'],
        # stage 3, 28x28 in
        ['ir_r1_k5_s2_e6_c80', 'ir_r3_k3_s1_e3_c80'],
        # stage 4, 14x14in
        ['ir_r1_k5_s1_e6_c96', 'ir_r3_k5_s1_e3_c96'],
        # stage 5, 14x14in
        ['ir_r4_k5_s2_e6_c192'],
        # stage 6, 7x7 in
        ['ir_r1_k3_s1_e6_c320_noskip']
    ]
    bn_momentum, bn_eps = _resolve_bn_params(kwargs)
    model = GenMobileNet(
        arch_def,
        num_classes=num_classes,
        stem_size=32,
        depth_multiplier=depth_multiplier,
        depth_divisor=8,
        min_depth=None,
        bn_momentum=bn_momentum,
        bn_eps=bn_eps,
        **kwargs
    )
    return model


def mnasnet_050(pretrained=False, **kwargs):
    """ MNASNet B1, depth multiplier of 0.5. """
    model = _gen_mnasnet_b1(0.5, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['mnasnet_050']))
    return model


def mnasnet_075(pretrained=False, **kwargs):
    """ MNASNet B1, depth multiplier of 0.75. """
    model = _gen_mnasnet_b1(0.75, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['mnasnet_075']))
    return model


def mnasnet_100(pretrained=False, **kwargs):
    """ MNASNet B1, depth multiplier of 1.0. """
    model = _gen_mnasnet_b1(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['mnasnet_100']))
    return model


def tflite_mnasnet_100(pretrained=False, **kwargs):
    """ MNASNet B1, depth multiplier of 1.0. """
    # these two args are for compat with tflite pretrained weights
    kwargs['folded_bn'] = True
    kwargs['padding_same'] = True
    model = _gen_mnasnet_b1(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['tflite_mnasnet_100']))
    return model


def mnasnet_140(pretrained=False, **kwargs):
    """ MNASNet B1,  depth multiplier of 1.4 """
    model = _gen_mnasnet_b1(1.4, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['mnasnet_140']))
    return model


def semnasnet_050(pretrained=False, **kwargs):
    """ MNASNet A1 (w/ SE), depth multiplier of 0.5 """
    model = _gen_mnasnet_a1(0.5, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['semnasnet_050']))
    return model


def semnasnet_075(pretrained=False, **kwargs):
    """ MNASNet A1 (w/ SE),  depth multiplier of 0.75. """
    model = _gen_mnasnet_a1(0.75, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['semnasnet_075']))
    return model


def semnasnet_100(pretrained=False, **kwargs):
    """ MNASNet A1 (w/ SE), depth multiplier of 1.0. """
    model = _gen_mnasnet_a1(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['semnasnet_100']))
    return model


def tflite_semnasnet_100(pretrained=False, **kwargs):
    """ MNASNet A1, depth multiplier of 1.0. """
    # these two args are for compat with tflite pretrained weights
    kwargs['folded_bn'] = True
    kwargs['padding_same'] = True
    model = _gen_mnasnet_a1(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['tflite_semnasnet_100']))
    return model


def semnasnet_140(pretrained=False, **kwargs):
    """ MNASNet A1 (w/ SE), depth multiplier of 1.4. """
    model = _gen_mnasnet_a1(1.4, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['semnasnet_140']))
    return model


def mnasnet_small(pretrained=False, **kwargs):
    """ MNASNet Small,  depth multiplier of 1.0. """
    model = _gen_mnasnet_small(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['mnasnet_small']))
    return model


def mobilenetv1_100(pretrained=False, **kwargs):
    """ MobileNet V1 """
    model = _gen_mobilenet_v1(1.0, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['mobilenetv1_100']))
    return model


def mobilenetv2_100(pretrained=False, **kwargs):
    """ MobileNet V2 """
    model = _gen_mobilenet_v2(1.0, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['mobilenetv2_100']))
    return model


def mobilenetv3_050(num_classes, in_chans=3, pretrained=False, **kwargs):
    """ MobileNet V3 """
    model = _gen_mobilenet_v3(0.5, num_classes=num_classes, in_chans=in_chans, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['mobilenetv3_050']))
    return model


def mobilenetv3_075(num_classes, in_chans=3, pretrained=False, **kwargs):
    """ MobileNet V3 """
    model = _gen_mobilenet_v3(0.75, num_classes=num_classes, in_chans=in_chans, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['mobilenetv3_075']))
    return model


def mobilenetv3_100(num_classes, in_chans=3, pretrained=False, **kwargs):
    """ MobileNet V3 """
    model = _gen_mobilenet_v3(1.0, num_classes=num_classes, in_chans=in_chans, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['mobilenetv3_100']))
    return model


def fbnetc_100(pretrained=False, **kwargs):
    """ FBNet-C """
    model = _gen_fbnetc(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['fbnetc_100']))
    return model


def chamnetv1_100(pretrained=False, **kwargs):
    """ ChamNet """
    model = _gen_chamnet_v1(1.0, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['chamnetv1_100']))
    return model


def chamnetv2_100(pretrained=False, **kwargs):
    """ ChamNet """
    model = _gen_chamnet_v2(1.0, **kwargs)
    #if pretrained:
    #    model.load_state_dict(load_state_dict_from_url(model_urls['chamnetv2_100']))
    return model


def spnasnet_100(pretrained=False, **kwargs):
    """ Single-Path NAS Pixel1"""
    model = _gen_spnasnet(1.0, **kwargs)
    if pretrained:
        model.load_state_dict(load_state_dict_from_url(model_urls['spnasnet_100']))
    return model
