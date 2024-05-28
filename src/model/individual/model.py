import os
from types import SimpleNamespace

import torch
import torch.nn.functional as F
from lightning.pytorch import LightningModule
from lightning.pytorch.callbacks import ModelCheckpoint

from .core import IndividualTemporalTransformer


class IndividualActivityRecognition(LightningModule):
    def __init__(self, config: SimpleNamespace):
        super().__init__()
        self.config = config
        self.data_type = config.data_type
        self.seq_len = config.seq_len
        self.lr = config.lr
        self.add_position_patch = config.add_position_patch
        h, w = config.img_size
        checkpoint_dir = (
            f"models/individual/seq_len{config.seq_len}-stride{config.stride}-{h}x{w}/"
        )
        os.makedirs(checkpoint_dir, exist_ok=True)
        self.callbacks = [
            ModelCheckpoint(
                checkpoint_dir,
                filename=f"individual_{self.data_type}_loss_min",
                monitor="l",
                mode="min",
                save_last=True,
            ),
        ]
        last_name = f"individual_{self.data_type}_last"
        self.callbacks[0].CHECKPOINT_NAME_LAST = last_name

        self.model = None

    def configure_model(self):
        config = self.config
        if self.model is not None:
            return
        self.model = IndividualTemporalTransformer(
            config.data_type,
            config.seq_len,
            config.hidden_ndim,
            config.latent_ndim,
            config.nheads,
            config.nlayers,
            config.emb_hidden_ndim,
            config.emb_nheads,
            config.emb_nlayers,
            config.dropout,
            config.emb_dropout,
            config.add_position_patch,
            config.patch_size,
            config.img_size,
        )

    def loss_func(self, x, fake_x, mu, log_sig, bboxs, fake_bboxs):
        x_rc = F.cross_entropy(x, fake_x)
        kl = -0.5 * torch.sum(1 + log_sig - mu**2 - log_sig.exp())
        loss = x_rc + kl
        logs = {"x": x_rc, "kl": kl}
        if self.add_position_patch:
            bbox_rc = F.cross_entropy(bboxs, fake_bboxs)
            loss += bbox_rc
            logs["b"] = bbox_rc
        logs["l"] = loss
        self.log_dict(logs, prog_bar=True, on_step=True, on_epoch=True)
        return loss

    def training_step(self, batch, batch_idx):
        if self.data_type == "keypoints":
            _, _, _, bboxs, x = batch
        elif self.data_type == "images":
            _, frames, flows, bboxs, _ = batch
            x = torch.cat([frames, flows], dim=2)
        if not self.add_position_patch:
            bboxs = None

        fake_x, z, mu, log_sig, fake_bboxs = self.model(x, bboxs)
        loss = self.loss_func(x, fake_x, mu, log_sig, bboxs, fake_bboxs)

        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)
