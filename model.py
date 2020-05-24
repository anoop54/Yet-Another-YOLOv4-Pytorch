import torch
from torch import nn
import torch.nn.functional as F

# Model consists of 3 parts
# - backbone
# - neck
# - head

# To implement:
# Mish (just download)
# CSP (is in architecture)
# MiWRC (attention_forward)
# SPP block (in architecture)
# PAN (in architecture)
# Implemented with https://lutzroeder.github.io/netron/?url=https%3A%2F%2Fraw.githubusercontent.com%2FAlexeyAB%2Fdarknet%2Fmaster%2Fcfg%2Fyolov4.cfg


class BadParams(Exception):
    pass

#Taken from https://github.com/lessw2020/mish
class Mish(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        #inlining this saves 1 second per epoch (V100 GPU) vs having a temp x and then returning x(!)
        return x *( torch.tanh(F.softplus(x)))

#Taken and modified from https://github.com/Tianxiaomo/pytorch-YOLOv4/blob/master/models.py
class ConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, activation, bn=True, bias=False):
        super().__init__()

        #PADDING is (ks-1)/2
        padding = (kernel_size - 1) // 2

        modules = []
        modules.append(nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding, bias=bias))
        if bn:
            modules.append(nn.BatchNorm2d(out_channels))
        if activation == "mish":
            modules.append(Mish())
        elif activation == "relu":
            modules.append(nn.ReLU(inplace=True))
        elif activation == "leaky":
            modules.append(nn.LeakyReLU(0.1, inplace=True))
        elif activation == "linear":
            pass
        else:
            raise BadParams("Please use on of suggested activations: mish, relu, leaky, linear.")

        self.module = nn.Sequential(*modules)

    def forward(self, x):
        return self.module(x)


#Taken and modified from https://github.com/Tianxiaomo/pytorch-YOLOv4/blob/master/models.py       
class ResBlock(nn.Module):
    """
    Sequential residual blocks each of which consists of \
    two convolution layers.
    Args:
        ch (int): number of input and output channels.
        nblocks (int): number of residual blocks.
        shortcut (bool): if True, residual tensor addition is enabled.
    """
    #Делаем несколько блоков, residual. Один блок состоит из двух свёрток, с ядрами 1 на 1 и 3 на 3
    def __init__(self, ch, nblocks=1, shortcut=True):
        super().__init__()
        self.shortcut = shortcut
        self.module_list = nn.ModuleList()
        for i in range(nblocks):
            resblock_one = nn.ModuleList()
            resblock_one.append(ConvBlock(ch, ch, 1, 1, 'mish'))
            resblock_one.append(ConvBlock(ch, ch, 3, 1, 'mish'))
            self.module_list.append(resblock_one)

    def forward(self, x):
        #Для каждого модуля проводим через residual слой
        for module in self.module_list:
            h = x
            for res in module:
                h = res(h)
            x = x + h if self.shortcut else h
        return x



class DownSampleFirst(nn.Module):
    """
    This is first downsample of the backbone model.
    It differs from the other stages, so it is written as another Module
    Args:
        in_channels (int): Amount of channels to input, if you use RGB, it should be 3
    """
    def __init__(self, in_channels = 3):
        super().__init__()

        self.c1 = ConvBlock(in_channels, 32, 3, 1, "mish")
        self.c2 = ConvBlock(32, 64, 3, 2, "mish")
        self.c3 = ConvBlock(64, 64, 1, 1, "mish")
        self.c4 = ConvBlock(64, 32, 1, 1, "mish")
        self.c5 = ConvBlock(32, 64, 3, 1, "mish")
        self.c6 = ConvBlock(64, 64, 1, 1, "mish")

        #CSP Layer
        self.dense_c3_c6 = ConvBlock(64, 64, 1, 1, "mish")

        self.c7 = ConvBlock(128, 64, 1, 1, "mish")

    def forward(self, x):
        x1 = self.c1(x)
        x2 = self.c2(x1)
        x3 = self.c3(x2) 
        x4 = self.c4(x3)
        x5 = self.c5(x4)
        x5 = x5 + x3    #Residual block
        x6 = self.c6(x5)
        xd6 = self.dense_c3_c6(x2) #CSP
        x6 = torch.cat([x6, xd6], dim=1)
        x7 = self.c7(x6)
        return x7

class DownSampleBlock(nn.Module):
    def __init__(self, in_c, out_c, nblocks=2):
        super().__init__()

        self.c1 = ConvBlock(in_c, out_c, 3, 2, "mish")
        self.c2 = ConvBlock(out_c, in_c, 1, 1, "mish")
        self.r3 = ResBlock(in_c, nblocks=nblocks)
        self.c4 = ConvBlock(in_c, in_c, 1, 1, "mish")

        #CSP Layer
        self.dense_c2_c4 = ConvBlock(out_c, in_c, 1, 1, "mish")

        self.c5 = ConvBlock(out_c, out_c, 1, 1, "mish")

    def forward(self, x):
        x1 = self.c1(x)
        x2 = self.c2(x1)
        x3 = self.r3(x2)
        x4 = self.c4(x3)
        xd4 = self.dense_c2_c4(x1)
        x4 = torch.cat([x4, xd4], dim=1)
        x5 = self.c5(x4)

        return x5



class Backbone(nn.Module):
    def __init__(self, in_channels):
        super().__init__()

        self.d1 = DownSampleFirst(in_channels=in_channels)
        self.d2 = DownSampleBlock(64, 128, nblocks=2)
        self.d3 = DownSampleBlock(128, 256, nblocks=8)
        self.d4 = DownSampleBlock(256, 512, nblocks=8)
        self.d5 = DownSampleBlock(512, 1024, nblocks=4)


    def forward(self, x):
        x1 = self.d1(x)
        x2 = self.d2(x1)
        x3 = self.d3(x2)
        x4 = self.d4(x3)
        x5 = self.d5(x4)

        return (x5, x4, x3)

class PAN_Layer(nn.Module):
    def __init__(self, in_channels):
        super().__init__()

        in_c = in_channels
        out_c = in_c // 2

        self.c1 = ConvBlock(in_c, out_c, 1, 1, "leaky")
        self.u2 = nn.Upsample(scale_factor=2, mode="nearest")
        #Gets input from d4
        self.c2_from_upsampled = ConvBlock(in_c, out_c, 1, 1, "leaky")
        #We use stack in PAN, so 512
        self.c3 = ConvBlock(in_c, out_c, 1, 1, "leaky")
        self.c4 = ConvBlock(out_c, in_c, 3, 1, "leaky")
        self.c5 = ConvBlock(in_c, out_c, 1, 1, "leaky")
        self.c6 = ConvBlock(out_c, in_c, 3, 1, "leaky")
        self.c7 = ConvBlock(in_c, out_c, 1, 1, "leaky")

    def forward(self, x_to_upsample, x_upsampled):
        x1 = self.c1(x_to_upsample)
        x2_1 = self.u2(x1)
        x2_2 = self.c2_from_upsampled(x_upsampled)
        #First is not upsampled!
        x2 = torch.cat([x2_2, x2_1], dim=1)
        x3 = self.c3(x2)
        x4 = self.c4(x3)
        x5 = self.c5(x4)
        x6 = self.c6(x5)
        x7 = self.c7(x6)
        return x7


class Neck(nn.Module):
    def __init__(self, spp_kernels = (5, 9, 13), PAN_layers = [512, 256]):
        super().__init__()

        self.c1 = ConvBlock(1024, 512, 1, 1, "leaky")
        self.c2 = ConvBlock(512, 1024, 3, 1, "leaky")
        self.c3 = ConvBlock(1024, 512, 1, 1, "leaky")

        #SPP block
        self.mp4_1 = nn.MaxPool2d(kernel_size=spp_kernels[0], stride=1, padding=spp_kernels[0] // 2)
        self.mp4_2 = nn.MaxPool2d(kernel_size=spp_kernels[1], stride=1, padding=spp_kernels[1] // 2)
        self.mp4_3 = nn.MaxPool2d(kernel_size=spp_kernels[2], stride=1, padding=spp_kernels[2] // 2)

        self.c5 = ConvBlock(2048, 512, 1, 1, "leaky")
        self.c6 = ConvBlock(512, 1024, 3, 1, "leaky")
        self.c7 = ConvBlock(1024, 512, 1, 1, "leaky")

        self.PAN8 = PAN_Layer(PAN_layers[0])
        self.PAN9 = PAN_Layer(PAN_layers[1])
    
    def forward(self, input):
        d5, d4, d3 = input

        x1 = self.c1(d5)
        x2 = self.c2(x1)
        x3 = self.c3(x2)

        x4_1 = self.mp4_1(x3)
        x4_2 = self.mp4_2(x3)
        x4_3 = self.mp4_3(x3)
        x4 = torch.cat([x4_1, x4_2, x4_3, x3], dim=1)

        x5 = self.c5(x4)
        x6 = self.c6(x5)
        x7 = self.c7(x6)

        x8 = self.PAN8(x7, d4)
        x9 = self.PAN9(x8, d3)

        return (x9, x8, x7)

class HeadPreprocessing(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        ic = in_channels
        self.c1 = ConvBlock(ic, ic*2, 3, 2, 'leaky')
        self.c2 = ConvBlock(ic*4, ic*2, 1, 1, 'leaky')
        self.c3 = ConvBlock(ic*2, ic*4, 3, 1, 'leaky')
        self.c4 = ConvBlock(ic*4, ic*2, 1, 1, 'leaky')
        self.c5 = ConvBlock(ic*2, ic*4, 3, 1, 'leaky')
        self.c6 = ConvBlock(ic*4, ic*2, 1, 1, 'leaky')

    def forward(self, input, input_prev):
        x1 = self.c1(input_prev)
        x1 = torch.cat([x1, input], dim=1)
        x2 = self.c2(x1)
        x3 = self.c3(x2)
        x4 = self.c4(x3)
        x5 = self.c5(x4)
        x6 = self.c6(x5)

        return x6

class HeadOutput(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.c1 = ConvBlock(in_channels, in_channels*2, 3, 1, "leaky")
        self.c2 = ConvBlock(in_channels*2, out_channels, 1, 1, "linear", bn=False, bias=True)
    
    def forward(self, x):
        x1 = self.c1(x)
        x2 = self.c2(x1)
        return x2

class Head(nn.Module):
    def __init__(self, output_ch):
        super().__init__()
        
        self.ho1 = HeadOutput(128, output_ch)
        
        self.hp2 = HeadPreprocessing(128)
        self.ho2 = HeadOutput(256, output_ch)

        self.hp3 = HeadPreprocessing(256)
        self.ho3 = HeadOutput(512, output_ch)

    def forward(self, input):
        input1, input2, input3 = input

        x1 = self.ho1(input1)
        x2 = self.hp2(input2, input1)
        x3 = self.ho2(x2)

        x4 = self.hp3(input3, x2)
        x5 = self.ho3(x4)

        return (x1, x3, x5)
        
class YOLOLayer(nn.Module):
    """Detection layer taken and modified from https://github.com/eriklindernoren/PyTorch-YOLOv3"""

    def __init__(self, anchors, num_classes, img_dim=416):
        super(YOLOLayer, self).__init__()
        self.anchors = anchors
        self.num_anchors = len(anchors)
        self.num_classes = num_classes
        self.ignore_thres = 0.5
        self.mse_loss = nn.MSELoss()
        self.bce_loss = nn.BCELoss()
        self.obj_scale = 1
        self.noobj_scale = 100
        self.metrics = {}
        self.img_dim = img_dim
        self.grid_size = 0  # grid size

    def compute_grid_offsets(self, grid_size, cuda=True):
        self.grid_size = grid_size
        g = self.grid_size
        FloatTensor = torch.cuda.FloatTensor if cuda else torch.FloatTensor
        self.stride = self.img_dim / self.grid_size
        # Calculate offsets for each grid
        self.grid_x = torch.arange(g).repeat(g, 1).view([1, 1, g, g]).type(FloatTensor)
        self.grid_y = torch.arange(g).repeat(g, 1).t().view([1, 1, g, g]).type(FloatTensor)
        self.scaled_anchors = FloatTensor([(a_w / self.stride, a_h / self.stride) for a_w, a_h in self.anchors])
        self.anchor_w = self.scaled_anchors[:, 0:1].view((1, self.num_anchors, 1, 1))
        self.anchor_h = self.scaled_anchors[:, 1:2].view((1, self.num_anchors, 1, 1))

    def forward(self, x, targets=None):

        # Tensors for cuda support
        FloatTensor = torch.cuda.FloatTensor if x.is_cuda else torch.FloatTensor
        LongTensor = torch.cuda.LongTensor if x.is_cuda else torch.LongTensor
        ByteTensor = torch.cuda.ByteTensor if x.is_cuda else torch.ByteTensor

        num_samples = x.size(0)
        grid_size = x.size(2)

        prediction = (
            x.view(num_samples, self.num_anchors, self.num_classes + 5, grid_size, grid_size)
            .permute(0, 1, 3, 4, 2)
            .contiguous()
        )

        # Get outputs
        x = torch.sigmoid(prediction[..., 0])  # Center x
        y = torch.sigmoid(prediction[..., 1])  # Center y
        w = prediction[..., 2]  # Width
        h = prediction[..., 3]  # Height
        pred_conf = torch.sigmoid(prediction[..., 4])  # Conf
        pred_cls = torch.sigmoid(prediction[..., 5:])  # Cls pred.

        # If grid size does not match current we compute new offsets
        if grid_size != self.grid_size:
            self.compute_grid_offsets(grid_size, cuda=x.is_cuda)

        # Add offset and scale with anchors
        pred_boxes = FloatTensor(prediction[..., :4].shape)
        pred_boxes[..., 0] = x.data + self.grid_x
        pred_boxes[..., 1] = y.data + self.grid_y
        pred_boxes[..., 2] = torch.exp(w.data) * self.anchor_w
        pred_boxes[..., 3] = torch.exp(h.data) * self.anchor_h

        output = torch.cat(
            (
                pred_boxes.view(num_samples, -1, 4) * self.stride,
                pred_conf.view(num_samples, -1, 1),
                pred_cls.view(num_samples, -1, self.num_classes),
            ),
            -1,
        )

        return output

class YOLOv4(nn.Module):
    def __init__(self, in_channels = 3, n_classes = 80, img_dim=608, anchors=None):
        super().__init__()
        if anchors is None:
            anchors = [[[10, 13], [16, 30], [33, 23]],
                       [[30, 61], [62, 45], [59, 119]],
                       [[116, 90], [156, 198], [373, 326]]]

        output_ch = (4 + 1 + n_classes) * 3
        self.img_dim = img_dim

        self.backbone = Backbone(in_channels)

        self.neck = Neck()

        self.head = Head(output_ch)

        self.yolo1 = YOLOLayer(anchors[0], n_classes, img_dim)
        self.yolo2 = YOLOLayer(anchors[1], n_classes, img_dim)
        self.yolo3 = YOLOLayer(anchors[2], n_classes, img_dim)

    def forward(self, x):
        b = self.backbone(x)
        n = self.neck(b)
        h = self.head(n)

        h1, h2, h3 = h

        out1 = self.yolo1(h1)
        out2 = self.yolo2(h2)
        out3 = self.yolo3(h3)

        return out1, out2, out3


if __name__ == "__main__":
    model = YOLOv4().cuda()
    x1 = torch.zeros((1, 3, 608, 608)).cuda()


    
    import time

    for i in range(5):
        t0 = time.time()
        y = model(x1)[0]
        t1 = time.time()
        print(t1 - t0)

    print(y.shape)









