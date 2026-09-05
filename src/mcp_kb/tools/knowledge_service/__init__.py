"""The KnowledgeService: shared engine backing all MCP tools.

Split into per-domain mixins (see the sibling ``_*.py`` modules); this file just
composes them into the single public ``KnowledgeService`` class so every method
still shares one ``self`` instance exactly as before the split.
"""
from __future__ import annotations

from ._architecture import ArchitectureMixin
from ._ask import AskMixin
from ._capabilities import CapabilitiesMixin
from ._code_intel import CodeIntelMixin
from ._contracts import ContractsMixin
from ._core import CoreMixin
from ._defect_feature import DefectFeatureMixin
from ._dependents import DependentsMixin
from ._inspection import InspectionMixin
from ._kt import KtMixin
from ._onboarding import OnboardingMixin
from ._quality import QualityMixin
from ._rca import RcaMixin
from ._search import SearchMixin
from ._service_overview import ServiceOverviewMixin


class KnowledgeService(CoreMixin, ServiceOverviewMixin, DefectFeatureMixin, SearchMixin, ArchitectureMixin, InspectionMixin, CapabilitiesMixin, DependentsMixin, OnboardingMixin, ContractsMixin, KtMixin, QualityMixin, CodeIntelMixin, RcaMixin, AskMixin):
    """Loads the graph + retriever once and serves all tool requests."""
