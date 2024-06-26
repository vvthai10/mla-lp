import math


import torch
from torch import nn


# Residual CLIP Adapter
class ClipAdapter(nn.Module):
    def __init__(self, c_in, bottleneck=768):
        super(ClipAdapter, self).__init__()
        self.fc1 = nn.Sequential(
            nn.Linear(c_in, bottleneck, bias=False),
            nn.LeakyReLU(inplace=False),
        )
        self.fc2 = nn.Sequential(
            nn.Linear(bottleneck, c_in, bias=False),
            nn.LeakyReLU(inplace=False),
        )

    def forward(self, x):
        x = self.fc1(x)
        y = self.fc2(x)
        return x, y


class CLIP_Inplanted(nn.Module):
    def __init__(
        self,
        clip_model,
        features,
        seg_reduce_dim=128,
        det_reduce_dim=768,
        decoder_heads=4,
        extra_blocks=0
    ):
        super().__init__()
        self.clipmodel = clip_model
        self.image_encoder = clip_model.visual
        self.features = features

        # Segment Adapter
        self.seg_adapters = nn.ModuleList(
            [ClipAdapter(1024, bottleneck=seg_reduce_dim) for i in range(len(features))]
        )

        # Classification Adapter
        self.det_adapters = nn.ModuleList(
            [ClipAdapter(1024, bottleneck=det_reduce_dim) for i in range(len(features))]
        )

        self.decoder = nn.ModuleList(
            [
                nn.TransformerEncoderLayer(d_model=seg_reduce_dim, nhead=decoder_heads)
                for _ in range(len(self.features))
            ]
        )

        self.extra_blocks = nn.ModuleList(
            [
                nn.TransformerEncoderLayer(d_model=seg_reduce_dim, nhead=decoder_heads)
                for _ in range(extra_blocks)
            ]
        )

        self.text_proj = nn.Linear(768, seg_reduce_dim, device="cuda:0")

    def forward(self, image):
        x = self.image_encoder.conv1(image)
        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)

        x = torch.cat(
            [
                self.image_encoder.class_embedding.to(x.dtype)
                + torch.zeros(
                    x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device
                ),
                x,
            ],
            dim=1,
        )
        x = x + self.image_encoder.positional_embedding.to(x.dtype)

        x = self.image_encoder.patch_dropout(x)
        x = self.image_encoder.ln_pre(x)

        x = x.permute(1, 0, 2)

        seg_patch_tokens = []
        det_patch_tokens = []

        for i in range(24):
            x = self.image_encoder.transformer.resblocks[i](x)

            if (i + 1) in self.features:

                seg_adapt_med, seg_adapt_out = self.seg_adapters[
                    self.features.index(i + 1)
                ](x[0])

                det_adapt_med, det_adapt_out = self.det_adapters[
                    self.features.index(i + 1)
                ](x[0])

                x[1] = 0.8 * x[1] + 0.1 * seg_adapt_out + 0.1 * det_adapt_out

                seg_patch_tokens.append(seg_adapt_med)
                det_patch_tokens.append(det_adapt_med)

        seg_patch_tokens = [
            seg_patch_tokens[t].permute(1, 0, 2) for t in range(len(seg_patch_tokens))
        ]

        det_patch_tokens = [
            det_patch_tokens[t].permute(1, 0, 2) for t in range(len(det_patch_tokens))
        ]

        return None, seg_patch_tokens, det_patch_tokens

    def decode(self, patch_tokens, text_features, ith):

        text = text_features.permute(1, 0)
        text = self.text_proj(text)
        text = text.permute(1, 0)  # [128, 2]

        x = patch_tokens  # [2, 290, 128]
        x = x.permute(1, 0, 2)
        x = self.decoder[ith](x)  # [290, 2, 128]
        x = x.permute(1, 0, 2)

        patch = x[:, 1:, :]  # [2, 289, 128]
        patch = patch / patch.norm(dim=-1, keepdim=True)  # [2, 289, 128]

        # [2, 289, 128] @ [128, 2]
        anomaly_map = 100 * patch @ text  # [2, 289, 2]
        return x, anomaly_map
