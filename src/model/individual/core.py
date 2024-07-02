from types import SimpleNamespace

import torch
import torch.nn as nn
import torch.nn.functional as F
from rotary_embedding_torch import RotaryEmbedding

from src.model.layers import MLP, IndividualEmbedding, TransformerEncoderBlock


class Q(nn.Module):
    def __init__(self, config: SimpleNamespace):
        super().__init__()
        self.hidden_ndim = config.hidden_ndim
        self.tau = config.tau

        self.emb = IndividualEmbedding(
            config.emb_hidden_ndim,
            config.hidden_ndim,
            config.emb_nheads,
            config.emb_nlayers,
            config.emb_dropout,
            config.patch_size,
            config.img_size,
        )

        self.qy_x_emb = nn.Sequential(
            nn.LayerNorm(config.seq_len * config.hidden_ndim),
            MLP(config.seq_len * config.hidden_ndim, config.hidden_ndim),
        )

        self.pe = RotaryEmbedding(config.hidden_ndim, learned_freq=False)

        self.encoders = nn.ModuleList(
            [
                TransformerEncoderBlock(
                    config.hidden_ndim, config.nheads, config.dropout
                )
                for _ in range(config.nlayers)
            ]
        )

        self.norm = nn.LayerNorm(config.hidden_ndim)

        self.qy_x = nn.Sequential(
            MLP(config.hidden_ndim, config.hidden_ndim),
            nn.Linear(config.hidden_ndim, config.n_clusters),
            nn.Softmax(dim=1),
        )

        self.ff_y = MLP(config.n_clusters, config.hidden_ndim)
        self.ff_mu = MLP(config.hidden_ndim, config.latent_ndim)
        self.ff_logvar = MLP(config.hidden_ndim, config.latent_ndim)

    def forward(self, x_vis, x_spc, mask, stage):
        # embedding
        b, seq_len = x_vis.size()[:2]
        x = self.emb(x_vis, x_spc)

        # embed and concat y
        y = self.qy_x_emb(x.clone().view(b, seq_len * self.hidden_ndim))
        y = y.view(b, 1, self.hidden_ndim)
        x = torch.cat([y, x], dim=1)
        cls_mask = torch.full((b, 1), False, dtype=torch.bool, requires_grad=False).to(
            next(self.parameters()).device
        )
        mask = torch.cat([cls_mask, mask], dim=1)

        # positional embedding
        x = self.pe.rotate_queries_or_keys(x, seq_dim=1)

        # x (b, seq_len+1, hidden_ndim)
        for layer in self.encoders:
            x, attn_w = layer(x, mask)
        # x (b, seq_len+1, hidden_ndim)

        x = self.norm(x)

        # q(y|x)
        y = x[:, 0, :].view(b, 1, self.hidden_ndim) + y
        y = self.qy_x(y.view(b, self.hidden_ndim))
        if stage == "train":
            y = F.gumbel_softmax(torch.log(y), self.tau, dim=1)

        # q(z|x, y)
        x = x[:, 1:, :]
        b, n_clusters = y.size()
        x = x * self.ff_y(y.clone().view(b, 1, n_clusters))
        mu = self.ff_mu(x)
        logvar = self.ff_logvar(x)
        ep = torch.randn_like(logvar)
        z = mu + torch.exp(logvar / 2) * ep
        # z, mu, log_sig (b, seq_len, latent_ndim)

        return z, mu, logvar, y


class Pz_y(nn.Module):
    def __init__(self, config: SimpleNamespace):
        super().__init__()

        self.ff = MLP(1, config.seq_len)
        self.emb = MLP(config.n_clusters, config.latent_ndim)
        self.pe = RotaryEmbedding(config.latent_ndim, learned_freq=False)

        self.encoders = nn.ModuleList(
            [
                TransformerEncoderBlock(
                    config.latent_ndim, config.nheads, config.dropout
                )
                for _ in range(config.nlayers)
            ]
        )

        self.norm = nn.LayerNorm(config.latent_ndim)
        self.ff_mu = MLP(config.latent_ndim, config.latent_ndim)
        self.ff_logvar = MLP(config.latent_ndim, config.latent_ndim)

    def forward(self, y, mask=None):
        b, n_clusters = y.size()
        y = self.ff(y.view(b, n_clusters, 1))
        y = y.permute(0, 2, 1)  # (b, seq_len, latent_ndim)
        y = self.emb(y)
        y = self.pe.rotate_queries_or_keys(y, seq_dim=1, offset=1)

        for layer in self.encoders:
            y, attn_w = layer(y, mask)

        y = self.norm(y)
        mu_prior = self.ff_mu(y)
        logvar_prior = self.ff_logvar(y)
        ep = torch.randn_like(logvar_prior)
        z_prior = mu_prior + torch.exp(logvar_prior / 2) * ep

        return z_prior, mu_prior, logvar_prior


class Px_z(nn.Module):
    def __init__(self, config: SimpleNamespace, vis_npatchs: int):
        super().__init__()
        self.seq_len = config.seq_len
        self.hidden_ndim = config.hidden_ndim
        self.latent_ndim = config.latent_ndim
        self.emb_hidden_ndim = config.emb_hidden_ndim
        self.img_size = config.img_size
        self.tau = config.tau
        self.vis_npatchs = vis_npatchs

        self.emb = MLP(config.latent_ndim, config.hidden_ndim)
        self.pe = RotaryEmbedding(config.hidden_ndim, learned_freq=False)
        # self.ff_z = MLP(config.latent_ndim, config.hidden_ndim)
        self.encoders = nn.ModuleList(
            [
                # TransformerDecoderBlock(
                TransformerEncoderBlock(
                    config.hidden_ndim, config.nheads, config.dropout
                )
                for _ in range(config.nlayers)
            ]
        )

        self.ff = nn.Sequential(
            nn.LayerNorm(config.hidden_ndim),
            MLP(config.hidden_ndim, config.emb_hidden_ndim * 2),
        )

        self.lin_vis = nn.Sequential(
            nn.LayerNorm(config.emb_hidden_ndim),
            MLP(config.emb_hidden_ndim),
            nn.Linear(config.emb_hidden_ndim, 17 * 2),
            nn.Tanh(),
        )

        self.lin_spc = nn.Sequential(
            nn.LayerNorm(config.emb_hidden_ndim),
            MLP(config.emb_hidden_ndim),
            nn.Linear(config.emb_hidden_ndim, 2 * 2),
            nn.Tanh(),
        )

    def forward(self, z, mask):
        b = z.size()[0]

        # embedding fake x
        fake_x = self.emb(z)
        fake_x = self.pe.rotate_queries_or_keys(fake_x, seq_dim=1, offset=1)

        # z = self.ff_z(z)

        for layer in self.encoders:
            # fake_x = layer(fake_x, z, mask)
            fake_x, attn_w = layer(fake_x, mask)
        # fake_x (b, seq_len, hidden_ndim)

        # reconstruct
        fake_x = self.ff(fake_x)
        fake_x_vis, fake_x_spc = (
            fake_x[:, :, : self.emb_hidden_ndim],
            fake_x[:, :, self.emb_hidden_ndim :],
        )
        # fake_x_vis, fake_x_spc (b, seq_len, emb_hidden_ndim)

        # reconstruct x_vis
        fake_x_vis = self.lin_vis(fake_x_vis)
        fake_x_vis = fake_x_vis.view(b, self.seq_len, 17, 2)

        # reconstruct x_spc
        fake_x_spc = self.lin_spc(fake_x_spc)
        fake_x_spc = fake_x_spc.view(b, self.seq_len, 2, 2)

        return fake_x_vis, fake_x_spc
