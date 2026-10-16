"""Translate legacy proof diagnostics only at the presentation boundary.

The original strings participate in compiled-action hashes and must remain
unchanged in stored proofs and in the range compiler.
"""
_RANGE_DIAGNOSTICS = {'不支持的文档片段类型': 'Unsupported document fragment type', '文档结构无法可靠定位文本范围': 'The document structure cannot reliably locate the text range', '文档块 ID 重复，无法可靠定位文本范围': 'Duplicate document block IDs prevent reliable text range location', '范围对齐超过验证预算，按整个文本块处理': 'Range alignment exceeded the verification limit; the entire text block is used', '重复内容存在多种对齐，已扩大到能够验证的范围': 'Repeated content has multiple possible alignments; the range was expanded to a verifiable boundary', '文档结构或属性变化，按整文档范围处理': 'Document structure or properties changed; the entire document is used', '段落对应位置存在歧义，按完整受影响段落组处理': 'Paragraph correspondence is ambiguous; the complete affected paragraph group is used'}

def public_range_reason(value: str | None) -> str | None:
    return _RANGE_DIAGNOSTICS.get(value, value)
