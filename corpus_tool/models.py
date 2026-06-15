"""数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict
import json


@dataclass
class Corpus:
    id: Optional[int] = None
    original_text: str = ""
    desensitized_text: str = ""
    source_file: str = ""
    status: str = "imported"
    rule_version: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    is_sampled: bool = False
    sample_batch: Optional[str] = None
    final_conclusion: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            original_text=row[1],
            desensitized_text=row[2],
            source_file=row[3],
            status=row[4],
            rule_version=row[5],
            created_at=row[6],
            updated_at=row[7],
            is_sampled=bool(row[8]),
            sample_batch=row[9],
            final_conclusion=row[10],
            metadata=json.loads(row[11]) if row[11] else {},
        )


@dataclass
class DesensitizationRule:
    id: Optional[int] = None
    name: str = ""
    category: str = ""
    pattern: str = ""
    replacement: str = ""
    version: int = 1
    is_active: int = 1
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    description: str = ""

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            name=row[1],
            category=row[2],
            pattern=row[3],
            replacement=row[4],
            version=row[5],
            created_at=row[6],
            description=row[7],
        )


@dataclass
class ReviewRecord:
    id: Optional[int] = None
    corpus_id: int = 0
    reviewer: str = ""
    conclusion: str = ""
    comment: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    rule_version_at_review: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            corpus_id=row[1],
            reviewer=row[2],
            conclusion=row[3],
            comment=row[4],
            created_at=row[5],
            rule_version_at_review=row[6],
        )


@dataclass
class AuditLog:
    id: Optional[int] = None
    operation: str = ""
    operator: str = ""
    details: str = ""
    rule_version: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            operation=row[1],
            operator=row[2],
            details=row[3],
            rule_version=row[4],
            created_at=row[5],
        )


@dataclass
class ConflictRecord:
    id: Optional[int] = None
    corpus_id: int = 0
    reviewer1: str = ""
    reviewer2: str = ""
    conclusion1: str = ""
    conclusion2: str = ""
    resolved: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            corpus_id=row[1],
            reviewer1=row[2],
            reviewer2=row[3],
            conclusion1=row[4],
            conclusion2=row[5],
            resolved=bool(row[6]),
            created_at=row[7],
        )


@dataclass
class Snapshot:
    id: Optional[int] = None
    name: str = ""
    description: str = ""
    rule_version: int = 0
    export_config_name: str = ""
    corpus_count: int = 0
    review_count: int = 0
    conflict_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    created_by: str = ""

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            name=row[1],
            description=row[2],
            rule_version=row[3],
            export_config_name=row[4],
            corpus_count=row[5],
            review_count=row[6],
            conflict_count=row[7],
            created_at=row[8],
            created_by=row[9],
        )


@dataclass
class ReleaseOrder:
    id: Optional[int] = None
    name: str = ""
    description: str = ""
    status: str = "draft"
    source_config_name: str = ""
    target_config_name: str = ""
    config_json: str = ""
    rule_version: int = 0
    approver: Optional[str] = None
    created_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    approved_at: Optional[str] = None
    published_at: Optional[str] = None

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            name=row[1],
            description=row[2],
            status=row[3],
            source_config_name=row[4],
            target_config_name=row[5],
            config_json=row[6],
            rule_version=row[7],
            approver=row[8],
            created_by=row[9],
            created_at=row[10],
            approved_at=row[11],
            published_at=row[12],
        )


@dataclass
class ReleaseOrderHistory:
    id: Optional[int] = None
    release_order_id: int = 0
    action: str = ""
    operator: str = ""
    details: str = ""
    from_config_json: Optional[str] = None
    to_config_json: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            release_order_id=row[1],
            action=row[2],
            operator=row[3],
            details=row[4],
            from_config_json=row[5],
            to_config_json=row[6],
            created_at=row[7],
        )
