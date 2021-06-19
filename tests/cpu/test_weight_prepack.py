import math
import random
import unittest
import itertools
import copy
import os

try:
    import torchvision
    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False
skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")

import torch
import intel_pytorch_extension as ipex
from torch.testing._internal.common_utils import TestCase


class TestPrepackCases(TestCase):
    def _test_convolution_training_base(self, dim):
        conv_module = {1: torch.nn.Conv1d, 2: torch.nn.Conv2d, 3: torch.nn.Conv3d}
        input_shapes = {1: (224,), 2: (224, 224), 3: (55, 55, 55)}
        options = itertools.product([True, False], [1, 2], [1, 4])
        for bias, dilation, groups in options:
            N = torch.randint(3, 10, (1,)).item()
            M = torch.randint(1, 3, (1,)).item() * groups
            C = torch.randint(1, 3, (1,)).item() * groups
            x_shape = (N, C) + input_shapes[dim]
            x = torch.randn(x_shape, dtype=torch.float32)

            model = conv_module[dim](in_channels=C,
                                    out_channels=M,
                                    kernel_size=3,
                                    stride=2,
                                    padding=1,
                                    dilation=dilation,
                                    bias=bias,
                                    groups=groups).float().train()

            model = model.to(memory_format=torch.channels_last)
            for dtype in [torch.float32, torch.bfloat16]:
                x = x.to(memory_format=torch.channels_last)
                x1 = x.clone().requires_grad_()
                x2 = x.clone().requires_grad_()
                origin_model = copy.deepcopy(model).train()
                origin_optimizer = torch.optim.SGD(origin_model.parameters(), lr=0.01, momentum=0.9)
                conf = ipex.AmpConf(dtype)
                ipex_model, ipex_optimizer = ipex.optimize(origin_model, dtype=dtype, optimizer=origin_optimizer, level='O1')
                ipex_model = ipex_model.train()
                # prepack's weight's dim need great than dim+2
                self.assertTrue(ipex_model.weight.dim() > dim +2)
                # for training case, weight's dtype always float.
                self.assertTrue(ipex_model.weight.dtype == torch.float32)
                with ipex.amp.autocast(enabled=True, configure=conf):
                    # original path
                    y1 = origin_model(x1)
                    loss1 = y1.sum()
                    origin_optimizer.zero_grad()
                    loss1.backward()
                    origin_optimizer.step()
                    # ipex path
                    y2 = ipex_model(x2)
                    loss2 = y2.sum()
                    ipex_optimizer.zero_grad()
                    loss2.backward()
                    ipex_optimizer.step()
                self.assertEqual(y1, y2)
                self.assertEqual(loss1, loss2)
                self.assertEqual(x1.grad, x2.grad)
                if bias:
                    self.assertEqual(origin_model.bias.grad, ipex_model.bias.grad)
                # compare origin_model parameters with origin_model parameters after grad updata
                origin_model_state = origin_model.state_dict()
                ipex_model_state = ipex_model.state_dict()
                for var_name in origin_model_state:
                    self.assertEqual(origin_model_state[var_name], ipex_model_state[var_name])
                # compare momentum_buffer in optimizer's state(sgd)
                # TODO: other optimizer.
                origin_oprimizer_state = origin_optimizer.state_dict()
                ipex_oprimizer_state = ipex_optimizer.state_dict()
                for var_name in origin_oprimizer_state:
                    if var_name == 'state':
                        self.assertEqual(origin_oprimizer_state[var_name], ipex_oprimizer_state[var_name])

    def test_conv2d(self):
        self._test_convolution_training_base(dim = 2)
        # TODO: add inference case.

    def test_model_serialization(self):
        model = torch.nn.Conv2d(3, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)
        model = model.to(memory_format=torch.channels_last).train()

        x = torch.randn(64, 3, 224, 224).to(memory_format=torch.channels_last)
        for dtype in [torch.float32, torch.bfloat16]:
            conf = ipex.AmpConf(dtype)
            origin_x = x.clone()
            ipex_x = x.clone()
            origin_model = copy.deepcopy(model).train()
            origin_optimizer = torch.optim.SGD(origin_model.parameters(), lr=0.01, momentum=0.9)
            ipex_model, ipex_optimizer = ipex.optimize(origin_model, dtype=dtype, optimizer=origin_optimizer, level='O1')
            ipex_model = ipex_model.train()
            with ipex.amp.autocast(enabled=True, configure=conf):
                # train one step for origin.
                y1 = origin_model(origin_x)
                loss1 = y1.sum()
                origin_optimizer.zero_grad()
                loss1.backward()
                origin_optimizer.step()
                # train one step for ipex.
                y2 = ipex_model(ipex_x)
                loss2 = y2.sum()
                ipex_optimizer.zero_grad()
                loss2.backward()
                ipex_optimizer.step()
            torch.save({'model_state_dict': origin_model.state_dict(),
                        'optimizer_state_dict': origin_optimizer.state_dict()
                        }, 'origin_checkpoint.pth')
            torch.save({'model_state_dict': ipex_model.state_dict(),
                        'optimizer_state_dict': ipex_optimizer.state_dict()
                        }, 'ipex_checkpoint.pth')
            self.assertEqual(y1, y2)
            self.assertEqual(loss1, loss2)
            origin_model_state = origin_model.state_dict()
            ipex_model_state = ipex_model.state_dict()
            for var_name in origin_model_state:
                self.assertEqual(origin_model_state[var_name], ipex_model_state[var_name])
            origin_model1 = copy.deepcopy(model).train()
            origin_optimizer1 = torch.optim.SGD(origin_model1.parameters(), lr=0.01, momentum=0.9)
            origin_checkpoint = torch.load('origin_checkpoint.pth')
            origin_model1.load_state_dict(origin_checkpoint['model_state_dict'])
            origin_optimizer1.load_state_dict(origin_checkpoint['optimizer_state_dict'])
            origin_model2 = copy.deepcopy(model)
            origin_optimizer2 = torch.optim.SGD(origin_model2.parameters(), lr=0.01, momentum=0.9)
            ipex_checkpoint = torch.load('ipex_checkpoint.pth')
            origin_model2.load_state_dict(ipex_checkpoint['model_state_dict'])
            origin_optimizer2.load_state_dict(ipex_checkpoint['optimizer_state_dict'])
            self.assertEqual(origin_model1.weight, origin_model2.weight)
            with ipex.amp.autocast(enabled=True, configure=conf):
                # train second step for origin.
                y1 = origin_model1(origin_x)
                loss1 = y1.sum()
                origin_optimizer1.zero_grad()
                loss1.backward()
                origin_optimizer1.step()
                # train second step for origin using ipex checkpoint.
                y2 = origin_model2(origin_x)
                loss2 = y2.sum()
                origin_optimizer2.zero_grad()
                loss2.backward()
                origin_optimizer2.step()
            self.assertEqual(y1, y2)
            self.assertEqual(loss1, loss2)
            self.assertEqual(origin_model1.weight, origin_model2.weight)
            os.remove('origin_checkpoint.pth')
            os.remove('ipex_checkpoint.pth')

    def _test_imagenet_model(self, model):
        model = model.to(memory_format=torch.channels_last)
        for dtype in [torch.float32, torch.bfloat16]:
            # inference case, will do conv+bn folding for 'O0' and 'O1'. will do weight' prepack for 'O1'.
            ipex_model1, _= ipex.optimize(model.eval(), dtype=dtype, level='O0')
            ipex_model2, _= ipex.optimize(model.eval(), dtype=dtype, level='O1')
            x = torch.randn(32, 3, 224, 224).to(memory_format=torch.channels_last)
            conf = ipex.AmpConf(dtype)
            with ipex.amp.autocast(enabled=True, configure=conf):
                y1 = ipex_model1(x)
                y2 = ipex_model2(x)
            self.assertEqual(y1, y2)
            # traing case.
            conf = ipex.AmpConf(dtype)
            origin_model = copy.deepcopy(model).train()
            origin_optimizer = torch.optim.SGD(origin_model.parameters(), lr=0.01, momentum=0.9)
            # do nothing for 'O0'
            ipex_model1, ipex_optimizer1= ipex.optimize(origin_model, dtype=dtype, optimizer=origin_optimizer, level='O0')
            # do weight prepack for 'O1'
            ipex_model2, ipex_optimizer2= ipex.optimize(origin_model, dtype=dtype, optimizer=origin_optimizer, level='O1')
            # run two iterations, and then compare the results.

            xx = [torch.randn(32, 3, 224, 224), torch.randn(32, 3, 224, 224)]
            for i in range(2):
                with ipex.amp.autocast(enabled=True, configure=conf):
                    x = xx[i]
                    # original case
                    y = origin_model(x)
                    loss = y.sum()
                    origin_optimizer.zero_grad()
                    loss.backward()
                    origin_optimizer.step()
                    # ipex case1.
                    y1 = ipex_model1(x)
                    loss1 = y1.sum()
                    ipex_optimizer1.zero_grad()
                    loss1.backward()
                    ipex_optimizer1.step()
                    # ipex case2.
                    y2 = ipex_model2(x)
                    loss2 = y2.sum()
                    ipex_optimizer2.zero_grad()
                    loss2.backward()
                    ipex_optimizer2.step()
            self.assertEqual(y, y1)
            self.assertEqual(y1, y2)
            self.assertEqual(loss, loss1)
            self.assertEqual(loss1, loss2)


    @skipIfNoTorchVision
    def test_resnet18(self):
        model = torchvision.models.resnet.resnet18(pretrained=False)
        self._test_imagenet_model(model)

    @skipIfNoTorchVision
    def test_resnext50_32x4d(self):
        model = torchvision.models.resnet.resnext50_32x4d(pretrained=False)
        self._test_imagenet_model(model)

if __name__ == '__main__':
    test = unittest.main()