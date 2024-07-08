# mypy: allow-untyped-defs
"""This module implements the user facing API for flex_attention in PyTorch."""
import functools
from typing import Callable, Optional, Tuple, Union

import torch
from torch._higher_order_ops.flex_attention import (
    flex_attention as flex_attention_hop,
    TransformGetItemToIndex,
)
from torch._higher_order_ops.utils import _set_compilation_env
from torch.fx.experimental.proxy_tensor import (
    _temp_remove_pre_dispatch_torch_function_mode,
)
from torch.nn.attention._utils import _validate_sdpa_input


def _compose(*fs):
    """Compose a sequence of score_mod functions."""

    def compose2(f, g):
        def inner(score, b, h, m, n):
            return f(g(score, b, h, m, n), b, h, m, n)

        return inner

    return functools.reduce(compose2, fs)


_score_mod_signature = Callable[
    [torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor], torch.Tensor
]

_mask_signature = Callable[
    [torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor], torch.Tensor
]


def _identity(
    score: torch.Tensor,
    batch: torch.Tensor,
    head: torch.Tensor,
    token_q: torch.Tensor,
    token_kv: torch.Tensor,
) -> torch.Tensor:
    return score


_DEFAULT_SPARSE_BLOCK_SIZE = 128


class _BlockMask:
    full_kv_num_blocks: torch.Tensor
    full_kv_indices: torch.Tensor
    full_q_num_blocks: torch.Tensor
    full_q_indices: torch.Tensor
    partial_kv_num_blocks: torch.Tensor
    partial_kv_indices: torch.Tensor
    partial_q_num_blocks: torch.Tensor
    partial_q_indices: torch.Tensor
    KV_BLOCK_SIZE: int
    Q_BLOCK_SIZE: int

    def __init__(
        self,
        full_kv_num_blocks,
        full_kv_indices,
        full_q_num_blocks,
        full_q_indices,
        partial_kv_num_blocks,
        partial_kv_indices,
        partial_q_num_blocks,
        partial_q_indices,
        KV_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
        Q_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
    ):
        self.full_kv_num_blocks = full_kv_num_blocks
        self.full_kv_indices = full_kv_indices
        self.full_q_num_blocks = full_q_num_blocks
        self.full_q_indices = full_q_indices
        self.partial_kv_num_blocks = partial_kv_num_blocks
        self.partial_kv_indices = partial_kv_indices
        self.partial_q_num_blocks = partial_q_num_blocks
        self.partial_q_indices = partial_q_indices
        self.KV_BLOCK_SIZE = KV_BLOCK_SIZE
        self.Q_BLOCK_SIZE = Q_BLOCK_SIZE

    def as_tuple(self):
        return (
            self.full_kv_num_blocks,
            self.full_kv_indices,
            self.full_q_num_blocks,
            self.full_q_indices,
            self.partial_kv_num_blocks,
            self.partial_kv_indices,
            self.partial_q_num_blocks,
            self.partial_q_indices,
            self.KV_BLOCK_SIZE,
            self.Q_BLOCK_SIZE,
        )

    def __str__(self):
        s = f"BlockMask(sparsity={self.sparsity():.2f}%, mask=\n"
        s += self.to_string()
        s += ")"
        return s

    # def sparsity(self) -> float:
    #     """
    #     Computes the percentage of blocks that are sparse (i.e. not computed)
    #     """
    #     dense_mask = self.to_dense()
    #     dense_ratio = ((dense_mask != 0).sum()) / dense_mask.numel()
    #     return 100 * (1 - dense_ratio)

    # def to_dense(self) -> torch.Tensor:
    #     """
    #     Returns a dense block that is equivalent to the block mask.
    #     """
    #     num_rows = self.kv_num_blocks.shape[-1]
    #     num_cols = self.q_num_blocks.shape[-1]
    #     batch, head = self.kv_num_blocks.shape[:2]
    #     device = self.kv_num_blocks.device
    #     assert batch == 1, head == 1

    #     def create_dense_one(kv_num_blocks, kv_indices):
    #         dense_mask = kv_indices.new_zeros(num_rows, num_cols + 1, dtype=torch.int32)

    #         row_indices = torch.arange(
    #             num_rows, dtype=torch.int, device=device
    #         ).unsqueeze(-1)
    #         col_indices = torch.arange(num_cols, dtype=torch.int, device=device)
    #         index_mask = col_indices < kv_num_blocks.unsqueeze(-1)

    #         # We write to one spot "out of bounds"
    #         valid_indices = torch.where(index_mask, kv_indices, num_cols)

    #         # set the values in 'a' to 1 where the indices are valid
    #         dense_mask[row_indices, valid_indices] = 1
    #         return dense_mask[:, :num_cols]

    #     out = create_dense_one(self.kv_num_blocks[0, 0], self.kv_indices[0, 0])
    #     return out

    # def to_string(self, grid_size=(20, 20)):
    #     """
    #     Returns a string representation of the block mask. Quite nifty.

    #     If grid_size is None, prints out an uncompressed version. Warning, it can be quite big!
    #     """
    #     dense_mask = self.to_dense()
    #     num_rows, num_cols = dense_mask.shape
    #     if isinstance(grid_size, int):
    #         max_rows = grid_size
    #         max_cols = grid_size
    #     elif grid_size is None:
    #         max_rows = num_rows
    #         max_cols = num_cols
    #     else:
    #         max_rows, max_cols = grid_size
    #     vis = ""

    #     def summarize_section(section):
    #         percentage = section.float().mean().item()
    #         if percentage == 1:
    #             return "█"
    #         elif percentage == 0:
    #             return " "
    #         else:
    #             return "░"

    #     def cdiv(a, b):
    #         return (a + (b - 1)) // b

    #     row_step = max(1, cdiv(num_rows, max_rows))
    #     col_step = max(1, cdiv(num_cols, max_cols))

    #     for r in range(0, num_rows, row_step):
    #         for c in range(0, num_cols, col_step):
    #             char = summarize_section(dense_mask[r : r + row_step, c : c + col_step])
    #             vis += char * 2
    #         vis += "\n"
    #     return vis


def broadcast_to_dim(x, dim):
    while x.dim() < dim:
        x = x.unsqueeze(0)
    return x


def _convert_mask_to_block_mask(
    mask,
    KV_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
):
    assert mask.dtype == torch.bool
    mask = broadcast_to_dim(mask, 4)
    B, H, Q, KV = mask.shape
    assert Q % Q_BLOCK_SIZE == 0
    assert KV % KV_BLOCK_SIZE == 0
    mask = mask.view(
        B, H, Q // Q_BLOCK_SIZE, Q_BLOCK_SIZE, KV // KV_BLOCK_SIZE, KV_BLOCK_SIZE
    )  # [B, H, Q//Q_BLOCK_SIZE, Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE, KV_BLOCK_SIZE]
    mask = mask.permute(
        0, 1, 2, 4, 3, 5
    )  # [B, H, Q//Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE, Q_BLOCK_SIZE, KV_BLOCK_SIZE]
    mask_block_sum = mask.sum(
        dim=[-2, -1]
    )  # [B, H, Q//Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE]
    full_unmask_block_sum = Q_BLOCK_SIZE * KV_BLOCK_SIZE
    full_unmask_block = mask_block_sum == full_unmask_block_sum
    partial_mask_block = (mask_block_sum > 0) & (mask_block_sum < full_unmask_block_sum)
    return full_unmask_block, partial_mask_block


def _convert_block_mask_to_mask(
    block_mask,
    KV_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
):
    assert block_mask.dim() == 4
    B, H, Q, KV = block_mask.shape
    block_mask = block_mask.expand(Q_BLOCK_SIZE, KV_BLOCK_SIZE, *block_mask.shape)
    block_mask = block_mask.permute(2, 3, 4, 0, 5, 1).reshape(
        B, H, Q * Q_BLOCK_SIZE, KV * KV_BLOCK_SIZE
    )
    return block_mask


def _create_block_mask_from_mask(
    mask: torch.Tensor,
    KV_BLOCK_SIZE: int = _DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE: int = _DEFAULT_SPARSE_BLOCK_SIZE,
) -> _BlockMask:
    full_blocks, partial_blocks = _convert_mask_to_block_mask(
        mask, KV_BLOCK_SIZE=KV_BLOCK_SIZE, Q_BLOCK_SIZE=Q_BLOCK_SIZE
    )

    def create_block_mask_obj(block_mask: torch.Tensor) -> Tuple:
        block_mask = block_mask.to(dtype=torch.int32)
        kv_num_blocks = block_mask.sum(dim=3)
        kv_indices = torch.argsort(block_mask, dim=3, descending=True, stable=True)
        q_num_blocks = block_mask.sum(dim=2)
        q_indices = torch.argsort(
            block_mask, dim=2, descending=True, stable=True
        ).permute(0, 1, 3, 2)
        return (
            kv_num_blocks.to(torch.int32).to(mask.device).contiguous(),
            kv_indices.to(torch.int32).to(mask.device).contiguous(),
            q_num_blocks.to(torch.int32).to(mask.device).contiguous(),
            q_indices.to(torch.int32).to(mask.device).contiguous(),
        )

    full_bm = create_block_mask_obj(full_blocks)
    partial_bm = create_block_mask_obj(partial_blocks)

    return _BlockMask(
        *full_bm,
        *partial_bm,
        KV_BLOCK_SIZE=KV_BLOCK_SIZE,
        Q_BLOCK_SIZE=Q_BLOCK_SIZE,
    )


def _create_mask_from_score_mod(
    score_mod: _score_mod_signature,
    B: int,
    H: int,
    M: int,
    N: int,
    device: str = "cuda",
):
    r"""This function creates a mask tensor from a score_mod function.

    Args:
        score_mod (Callable): Function to modify attention scores.
        B (int): Batch size.
        H (int): Number of heads.
        M (int): Sequence length of query.
        N (int): Sequence length of key/value.
        device (str): Device to run the mask creation on.

    Returns:
        mask (Tensor): A mask tensor with shape (B, H, M, N).
    """
    b = torch.arange(0, B, device=device)
    h = torch.arange(0, H, device=device)
    m = torch.arange(0, M, device=device)
    n = torch.arange(0, N, device=device)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, None, 0))
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, 0, None))
    score_mod = torch.vmap(score_mod, in_dims=(0, None, 0, None, None))
    score_mod = torch.vmap(score_mod, in_dims=(0, 0, None, None, None))
    with TransformGetItemToIndex():
        out = score_mod(torch.zeros(B, H, M, N, device=device), b, h, m, n)
        mask = torch.where(torch.isinf(out), False, True)
    return mask


def _create_mask_from_score_mod(
    score_mod: _score_mod_signature,
    B: int,
    H: int,
    M: int,
    N: int,
    device: str = "cuda",
):
    r"""This function creates a mask tensor from a score_mod function.
    Args:
        score_mod (Callable): Function to modify attention scores.
        B (int): Batch size.
        H (int): Number of heads.
        M (int): Sequence length of query.
        N (int): Sequence length of key/value.
        device (str): Device to run the mask creation on.
    Returns:
        mask (Tensor): A mask tensor with shape (B, H, M, N).
    """
    b = torch.arange(0, B, device=device)
    h = torch.arange(0, H, device=device)
    m = torch.arange(0, M, device=device)
    n = torch.arange(0, N, device=device)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, None, 0))
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, 0, None))
    score_mod = torch.vmap(score_mod, in_dims=(0, None, 0, None, None))
    score_mod = torch.vmap(score_mod, in_dims=(0, 0, None, None, None))
    with TransformGetItemToIndex():
        out = score_mod(torch.zeros(B, H, M, N, device=device), b, h, m, n)
        mask = torch.where(torch.isinf(out), False, True)
    return mask


def _create_mask_from_mask_fn(
    mask_fn: _mask_signature,
    B: int,
    H: int,
    M: int,
    N: int,
    device: str = "cuda",
):
    r"""This function creates a mask tensor from a score_mod function.

    Args:
        mask_fn (Callable): Mask function.
        B (int): Batch size.
        H (int): Number of heads.
        M (int): Sequence length of query.
        N (int): Sequence length of key/value.
        device (str): Device to run the mask creation on.

    Returns:
        mask (Tensor): A mask tensor with shape (B, H, M, N).
    """
    b = torch.arange(0, B, device=device)
    h = torch.arange(0, H, device=device)
    m = torch.arange(0, M, device=device)
    n = torch.arange(0, N, device=device)
    mask_fn = torch.vmap(mask_fn, in_dims=(None, None, None, 0))
    mask_fn = torch.vmap(mask_fn, in_dims=(None, None, 0, None))
    mask_fn = torch.vmap(mask_fn, in_dims=(None, 0, None, None))
    mask_fn = torch.vmap(mask_fn, in_dims=(0, None, None, None))
    return mask_fn(b, h, m, n)


def _create_block_mask(
    fn: Union[_score_mod_signature, _mask_signature],
    B: int,
    H: int,
    M: int,
    N: int,
    device: str = "cuda",
    KV_BLOCK_SIZE: int = _DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE: int = _DEFAULT_SPARSE_BLOCK_SIZE,
    is_score_mod: bool = True,
):
    r"""This function creates a block mask tuple from a score_mod function.

    Args:
        mask_fn (Callable): Mask function.
        B (int): Batch size.
        H (int): Number of heads.
        M (int): Sequence length of query.
        N (int): Sequence length of key/value.
        device (str): Device to run the mask creation on.
        KV_BLOCK_SIZE (int): Block size of block mask for each query.
        Q_BLOCK_SIZE (int): Block size of block mask for each key/value.

    Returns:
        block_mask (tuple): A tuple of (kv_num_blocks, kv_indices, q_num_blocks, q_indices,
                            KV_BLOCK_SIZE, Q_BLOCK_SIZE) which represents the block mask.
    """
    if is_score_mod:
        mask = _create_mask_from_score_mod(fn, B, H, M, N, device)
    else:
        mask = _create_mask_from_mask_fn(fn, B, H, M, N, device)
    block_mask = _create_block_mask_from_mask(mask, KV_BLOCK_SIZE, Q_BLOCK_SIZE)
    return block_mask


"""
    The flex attention kernels are implemented using block sparsity,
    where only the unmasked blocks are computed to get the best perf.
    If users don't specify any block sparse mask info, we create this
    empty block sparse mask with all blocks unmasked as the default one.
"""


def _create_empty_block_mask(query, key, value) -> _BlockMask:
    device = query.device
    kv_len = key.size()[-2]
    q_len = query.size()[-2]
    return _BlockMask(
        full_kv_num_blocks=torch.ones([1, 1, 1], dtype=torch.int32, device=device),
        full_kv_indices=torch.zeros([1, 1, 1, 1], dtype=torch.int32, device=device),
        full_q_num_blocks=torch.ones([1, 1, 1], dtype=torch.int32, device=device),
        full_q_indices=torch.zeros([1, 1, 1, 1], dtype=torch.int32, device=device),
        partial_kv_num_blocks=torch.zeros([1, 1, 1], dtype=torch.int32, device=device),
        partial_kv_indices=torch.zeros([1, 1, 1, 1], dtype=torch.int32, device=device),
        partial_q_num_blocks=torch.zeros([1, 1, 1], dtype=torch.int32, device=device),
        partial_q_indices=torch.zeros([1, 1, 1, 1], dtype=torch.int32, device=device),
        KV_BLOCK_SIZE=kv_len,
        Q_BLOCK_SIZE=q_len,
    )


def _no_mask(
    batch: torch.Tensor,
    head: torch.Tensor,
    token_q: torch.Tensor,
    token_kv: torch.Tensor,
) -> torch.Tensor:
    return token_q >= 0


def _flex_attention(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: _score_mod_signature = _identity,
    block_mask: Optional[_BlockMask] = None,
    mask_fn: _mask_signature = _no_mask,
) -> torch.Tensor:
    r"""This function implements scaled dot product attention with an arbitrary attention score modification function.

    This function computes the scaled dot product attention between query, key, and value tensors with a user-defined
    attention score modification function. The attention score modification function will be applied after the attention
    scores have been calculated between the query and key tensors. The attention scores are calculated as follows:

    The ``score_mod`` function should have the following signature:

    .. code-block:: python

        def score_mod(
            score: torch.Tensor,
            batch: torch.Tensor,
            head: torch.Tensor,
            token_q: torch.Tensor,
            token_kv: torch.Tensor
        ) -> torch.Tensor:

    Where:
        - ``score``: A scalar tensor representing the attention score,
          with the same data type and device as the query, key, and value tensors.
        - ``batch``, ``head``, ``token_q``, ``token_kv``: Scalar tensors indicating
          the batch index, head index, query index, and key/value index, respectively.
          These should have the ``torch.int`` data type and be located on the same device as the score tensor.

    Args:
        query (Tensor): Query tensor; shape :math:`(B, H, L, E)`.
        key (Tensor): Key tensor; shape :math:`(B, H, S, E)`.
        value (Tensor): Value tensor; shape :math:`(B, H, S, Ev)`.
        score_mod (Callable): Function to modify attention scores. By default no score_mod is applied.

    Returns:
        output (Tensor): Attention output; shape :math:`(B, H, L, Ev)`.

    Shape legend:
        - :math:`N: \text{Batch size} ... : \text{Any number of other batch dimensions (optional)}`
        - :math:`S: \text{Source sequence length}`
        - :math:`L: \text{Target sequence length}`
        - :math:`E: \text{Embedding dimension of the query and key}`
        - :math:`Ev: \text{Embedding dimension of the value}`

    .. warning::
        `torch.nn.attention.flex_attention` is a prototype feature in PyTorch. It doesn't support training currently.
        Please look forward to a more stable implementation in a future version of PyTorch.
        Read more about feature classification at: https://pytorch.org/blog/pytorch-feature-classification-changes/#prototype

    """

    if block_mask is None:
        block_mask = _create_empty_block_mask(query, key, value)
    if torch.compiler.is_dynamo_compiling():
        # mark head_dim always to be static
        for x in [query, key, value]:
            torch._dynamo.mark_static(x, -1)
        out, _ = flex_attention_hop(
            query,
            key,
            value,
            score_mod,
            block_mask.as_tuple(),
            mask_fn,
        )
        return out

    # Some basic input validation
    _validate_sdpa_input(query, key, value)
    if query.size(-2) % 128 != 0:
        raise ValueError("NYI: S and L must be a multiple of 128")

    if not torch._dynamo.is_dynamo_supported():
        raise RuntimeError("flex_attention requires dynamo support.")

    with _set_compilation_env():
        with torch._dynamo.utils.disable_cache_limit():
            with _temp_remove_pre_dispatch_torch_function_mode():
                out, _ = torch.compile(
                    flex_attention_hop, backend="eager", fullgraph=True
                )(
                    query,
                    key,
                    value,
                    score_mod,
                    block_mask.as_tuple(),
                    mask_fn,
                )
                return out


"""Some common used score_mod functions for flex_attention in PyTorch."""


def _causal(
    score: torch.Tensor,
    batch: torch.Tensor,
    head: torch.Tensor,
    token_q: torch.Tensor,
    token_kv: torch.Tensor,
) -> torch.Tensor:
    return torch.where(token_q >= token_kv, score, float("-inf"))


def _rel_bias(
    score: torch.Tensor,
    batch: torch.Tensor,
    head: torch.Tensor,
    token_q: torch.Tensor,
    token_kv: torch.Tensor,
) -> torch.Tensor:
    return score + (token_q - token_kv)


def _rel_causal(
    score: torch.Tensor,
    batch: torch.Tensor,
    head: torch.Tensor,
    token_q: torch.Tensor,
    token_kv: torch.Tensor,
) -> torch.Tensor:
    return torch.where(token_q >= token_kv, score + (token_q - token_kv), float("-inf"))


def _generate_alibi_bias(num_heads: int):
    def _alibi_bias(
        score: torch.Tensor,
        batch: torch.Tensor,
        head: torch.Tensor,
        token_q: torch.Tensor,
        token_kv: torch.Tensor,
    ) -> torch.Tensor:
        scale = torch.exp2(-((head + 1) * 8.0 / num_heads))
        return score + (token_kv - token_q) * scale

    return _alibi_bias
