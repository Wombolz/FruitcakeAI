"""Bound retained review input without treating bytes as two tokens each."""

PROTOCOL_TOKEN_RESERVE = 8192
MAX_INPUT_BYTES = 512 * 1024


def input_byte_budget(model):
    # Budget one UTF-8 byte per remaining token, rather than the usual optimistic
    # chars-per-token estimate. This is a defensive bound, not exact tokenization.
    remaining = model.context_window - model.max_tokens - PROTOCOL_TOKEN_RESERVE
    return min(MAX_INPUT_BYTES, max(0, remaining))
