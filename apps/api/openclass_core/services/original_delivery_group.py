"""Combine current-turn original grants without eagerly loading their pages."""
from __future__ import annotations
from dataclasses import replace
from openclass_core.services.original_source_delivery import OriginalSourceBatch

class OriginalDeliveryGroup:
    """A delivery protocol shared by lazy sources and verified attachments.

    This object carries authorization, not a summary or cached teaching text.
    Each selected child is revalidated immediately before yielding its originals.
    """

    def __init__(self, deliveries):
        self.deliveries = tuple(deliveries)
        if not self.deliveries:
            raise ValueError('An original delivery group cannot be empty.')
        first = self.deliveries[0]
        self.owner_user_id, self.package_id = (first.owner_user_id, first.package_id)
        self.evidence_items = []
        known = {}
        from openclass_core.services.original_source_rehydration import _unit_key
        for delivery in self.deliveries:
            if (delivery.owner_user_id, delivery.package_id) != (self.owner_user_id, self.package_id):
                raise ValueError('Original attachments and sources must belong to the same owner and course.')
            for item in delivery.evidence_items:
                fingerprint = _unit_key(item)
                if item.id in known and known[item.id] != fingerprint:
                    raise ValueError('Repeated original identities have different source bytes or ranges.')
                if item.id not in known:
                    known[item.id] = fingerprint
                    self.evidence_items.append(item)
        self.validate_current()

    def validate_current(self):
        for delivery in self.deliveries:
            delivery.validate_current()
        return self

    def iter_batches(self, unit_ids=None):
        known = {item.id for item in self.evidence_items}
        selected = known if unit_ids is None else set(unit_ids)
        if not selected <= known:
            raise ValueError("Requested originals exceed this turn's authorized source and attachment scope.")
        self.validate_current()
        remaining = set(selected)
        for delivery in self.deliveries:
            chosen = remaining & {item.id for item in delivery.evidence_items}
            if not chosen:
                continue
            if isinstance(delivery, OriginalSourceBatch):
                items = [item for item in delivery.evidence_items if item.id in chosen]
                batches = (_subset(delivery, {item.id for item in items[start:start + 6]}) for start in range(0, len(items), 6))
            else:
                batches = delivery.iter_batches(unit_ids=chosen)
            for batch in batches:
                received = {item.id for item in batch.evidence_items}
                if not received or not received <= chosen:
                    raise ValueError('A source delivery returned originals outside its requested scope.')
                self.validate_current()
                yield batch.validate_current()
                remaining.difference_update(received)
        if remaining:
            raise ValueError('Some authorized source or attachment originals were not delivered.')

def _subset(batch, ids):
    pairs = [(image, entry) for image, entry in zip(batch.image_inputs, batch.image_manifest, strict=True) if entry['evidence_id'] in ids]
    return replace(batch, evidence_items=[item for item in batch.evidence_items if item.id in ids], image_inputs=[image for image, _ in pairs], image_manifest=[{**entry, 'image_index': index} for index, (_, entry) in enumerate(pairs)], visual_inputs=tuple(((visual, image) for visual, image in batch.visual_inputs if visual.metadata.get('source_unit_id') in ids))).validate_current()
