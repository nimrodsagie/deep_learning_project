import torch
from typing import Optional, Union, List
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.model_zoo as model_zoo
from segmentation_models_pytorch.encoders.resnet import resnet_encoders
from segmentation_models_pytorch.encoders.dpn import dpn_encoders
from segmentation_models_pytorch.encoders.vgg import vgg_encoders
from segmentation_models_pytorch.encoders.senet import senet_encoders
from segmentation_models_pytorch.encoders.densenet import densenet_encoders
from segmentation_models_pytorch.encoders.inceptionresnetv2 import inceptionresnetv2_encoders
from segmentation_models_pytorch.encoders.inceptionv4 import inceptionv4_encoders
from segmentation_models_pytorch.encoders.efficientnet import efficient_net_encoders
from segmentation_models_pytorch.encoders.mobilenet import mobilenet_encoders
from segmentation_models_pytorch.encoders.xception import xception_encoders


encoders = {}
encoders.update(resnet_encoders)
encoders.update(dpn_encoders)
encoders.update(vgg_encoders)
encoders.update(senet_encoders)
encoders.update(densenet_encoders)
encoders.update(inceptionresnetv2_encoders)
encoders.update(inceptionv4_encoders)
encoders.update(efficient_net_encoders)
encoders.update(mobilenet_encoders)
encoders.update(xception_encoders)

class CenterBlock(nn.Sequential):
    def __init__(self, in_channels, out_channels, use_batchnorm):
        conv1 = DoubleConvBlock(in_channels, out_channels, kernel_size=3, stride=1, padding=1,
                                use_batchnorm=use_batchnorm)
        super().__init__(conv1)

class Identity(nn.Module):
    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, x):
        return x

class DecoderBlock(nn.Module):
    def __init__(self, in_channels, out_channels, use_batchnorm=True, use_transpose_conv=True, mode='nearest'):
        super().__init__()

        self.mode = mode
        self.use_transpose_conv = use_transpose_conv
        self.upconv = nn.ConvTranspose2d(in_channels, int(in_channels/2), kernel_size=2, stride=2)
        if use_transpose_conv:
            self.conv = DoubleConvBlock(int(in_channels/2), out_channels, kernel_size=3, stride=1, padding=1,
                                        use_batchnorm=use_batchnorm)
        else:
            self.conv = DoubleConvBlock(in_channels, out_channels, kernel_size=3, stride=1, padding=1,
                                        use_batchnorm=use_batchnorm)

    def forward(self, x):
        if self.use_transpose_conv:
            x = self.upconv(x)
        else:
            x = F.interpolate(x, scale_factor=2, mode=self.mode)
        x = self.conv(x)
        return x

class SegmentationHead(nn.Sequential):

    def __init__(self, in_channels, out_channels, activation=None, kernel_size=3):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, padding=kernel_size // 2)
        if activation is None or activation == 'identity':
            self.activation = Identity()
        elif activation == 'sigmoid':
            self.activation = nn.Sigmoid()
        elif activation == 'softmax2d':
            self.activation = nn.Softmax(dim=1)
        elif activation == 'softmax':
            self.activation = nn.Softmax()
        elif activation == 'logsoftmax':
            self.activation = nn.LogSoftmax()
        else:
            raise ValueError('Activation should be sigmoid/softmax/logsoftmax/None; got {}'.format(activation))

    def forward(self, x):
        x = self.conv(x)
        return self.activation(x)

class UnetDecoder2D(nn.Module):
    def __init__(self, encoder_channels, decoder_channels, n_blocks=5, use_batchnorm=True, center=False):
        super().__init__()
        if n_blocks != len(decoder_channels):
            raise ValueError("Model depth is {}, but you provide `decoder_channels` for {} blocks.".format(n_blocks,
                                                                                                           len(decoder_channels)))
        encoder_channels = encoder_channels[1:]  # remove first skip with same spatial resolution
        encoder_channels = encoder_channels[::-1]  # reverse channels to start from head of encoder
        head_channels = encoder_channels[0]
        in_channels = [head_channels] + list(decoder_channels)
        out_channels = decoder_channels
        if center:
            self.center = CenterBlock(head_channels, head_channels, use_batchnorm=use_batchnorm)
        else:
            self.center = Identity()
        blocks = [DecoderBlock(in_ch, out_ch)
            for in_ch, out_ch in zip(in_channels, out_channels)]
        self.blocks = nn.ModuleList(blocks)

    def forward(self, *features):

        features = features[1:]  # remove first skip with same spatial resolution
        features = features[::-1]  # reverse channels to start from head of encoder

        head = features[0]

        x = self.center(head)
        for i, decoder_block in enumerate(self.blocks):
            x = decoder_block(x)

        return x

class DoubleConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1, use_batchnorm=True):
        super(DoubleConvBlock, self).__init__()
        if use_batchnorm:
            self.conv = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size,
                          stride=stride, padding=padding),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(inplace=True),
                nn.Conv2d(out_channels, out_channels, kernel_size=kernel_size,
                          stride=stride, padding=padding),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(inplace=True))
        else:
            self.conv = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size,
                          stride=stride, padding=padding),
                nn.ReLU(inplace=True),
                nn.Conv2d(out_channels, out_channels, kernel_size=kernel_size,
                          stride=stride, padding=padding),
                nn.ReLU(inplace=True))

    def forward(self, x):
        x = self.conv(x)
        return x

class SegmentationModel(torch.nn.Module):

    def initialize(self):
        self.initialize_decoder(self.decoder)
        self.initialize_head(self.segmentation_head)

    def forward(self, x):
        """Sequentially pass `x` trough model`s encoder, decoder and heads"""
        features = self.encoder(x)
        decoder_output = self.decoder(*features)

        masks = self.segmentation_head(decoder_output)

        if self.classification_head is not None:
            labels = self.classification_head(features[-1])
            return masks, labels

        return masks

    def predict(self, x):
        """Inference method. Switch model to `eval` mode, call `.forward(x)` with `torch.no_grad()`
        Args:
            x: 4D torch tensor with shape (batch_size, channels, height, width)
        Return:
            prediction: 4D torch tensor with shape (batch_size, classes, height, width)
        """
        if self.training:
            self.eval()

        with torch.no_grad():
            x = self.forward(x)

        return x

    def initialize_decoder(self, module):
        for m in module.modules():

            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_uniform_(m.weight, mode="fan_in", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

            elif isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def initialize_head(self, module):
        for m in module.modules():
            if isinstance(m, (nn.Linear, nn.Conv2d)):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

class Unet_2D(SegmentationModel):
    def __init__(self,
                 encoder_name: str = "resnet34",
                 encoder_depth: int = 5,
                 encoder_weights: str = "imagenet",
                 decoder_use_batchnorm: bool = True,
                 decoder_channels: List[int] = (256, 128, 64, 32, 16),
                 in_channels: int = 3,
                 classes: int = 2,
                 activation: str = 'softmax'):
        super(Unet_2D, self).__init__()

        # encoder
        self.encoder = self.get_encoder(encoder_name, in_channels=in_channels, depth=encoder_depth, weights=encoder_weights)

        # decoder
        self.decoder = UnetDecoder2D(encoder_channels=self.encoder.out_channels, decoder_channels=decoder_channels,
                                     n_blocks=encoder_depth, use_batchnorm=decoder_use_batchnorm,
                                     center=True if encoder_name.startswith("vgg") else False)

        self.segmentation_head = SegmentationHead(in_channels=decoder_channels[-1],
                                                  out_channels=classes,
                                                  activation=activation,
                                                  kernel_size=3)

        self.name = 'u-{}'.format(encoder_name)
        self.initialize()

    def forward(self, x):
        features = self.encoder(x)
        x = self.decoder(*features)
        output = self.segmentation_head(x)
        return output


    def get_encoder(self, name, in_channels=3, depth=5, weights=None):
        Encoder = encoders[name]["encoder"]
        params = encoders[name]["params"]
        params.update(depth=depth)
        encoder = Encoder(**params)

        if weights is not None:
            settings = encoders[name]["pretrained_settings"][weights]
            encoder.load_state_dict(model_zoo.load_url(settings["url"]))

        encoder.set_in_channels(in_channels)

        return encoder







