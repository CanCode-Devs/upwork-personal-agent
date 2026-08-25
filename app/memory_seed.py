from __future__ import annotations

from typing import TypedDict

from sqlalchemy.orm import Session

from app.db.models import PortfolioItem
from app.models import WorkOrigin
from app.tools.memory import update_portfolio_matrix


class AgentCaseStudy(TypedDict):
    project_title: str
    tech_stack: list[str]
    outcomes_achieved: str
    associated_keywords: list[str]
    description: str


CASE_STUDIES: list[AgentCaseStudy] = [
    {
        "project_title": "Medical Document Entity Extraction",
        "tech_stack": ["Python", "LangGraph", "OpenAI", "QWEN3", "Docker", "AWS"],
        "outcomes_achieved": "Accuracy 84% → 88%; weekly retraining removed; ~60% less manual review.",
        "associated_keywords": ["OCR", "NER", "document AI", "insurance", "medical", "LLM"],
        "description": (
            "Multimodal extraction pipeline for mixed print/handwriting medical and insurance forms. "
            "LangGraph orchestration with provider fallbacks, multi-page entity resolution, and validation gates. "
            "Shipped on AWS with GitHub Actions CI/CD."
        ),
    },
    {
        "project_title": "Underwriter's Assistant Chatbot",
        "tech_stack": ["LangGraph", "Milvus", "SQL", "AWS", "Docker"],
        "outcomes_achieved": "Multi-step underwriting lookups in under 30 seconds from one interface.",
        "associated_keywords": ["RAG", "agentic", "chatbot", "vector search", "insurance"],
        "description": (
            "Agentic chatbot that retrieves from Milvus and SQL, chains similar-case search with policy lookup, "
            "and returns grounded answers so underwriters stop switching tools."
        ),
    },
    {
        "project_title": "End-to-End Document Intelligence Pipeline",
        "tech_stack": ["DBNET", "EAST", "CRNN", "trOCR", "MobileNetV3", "AWS Kubernetes"],
        "outcomes_achieved": "8-stage production pipeline: 99% handwriting localization, 94–96% recognition, 39% fewer cross-page errors.",
        "associated_keywords": ["computer vision", "OCR", "handwriting", "forms", "document intelligence"],
        "description": (
            "Production document platform: handwriting localization, text recognition, handcheck detection, "
            "beam-search LM post-processing, value codification, and interpage correlation. Independent Docker "
            "stages on AWS Kubernetes."
        ),
    },
    {
        "project_title": "Local LLM Deployment & Serving Infrastructure",
        "tech_stack": ["vLLM", "GPU serving", "Python", "self-hosted LLM"],
        "outcomes_achieved": "Two production LLM instances; inference cost ~$0.35 → ~$0.10 per document.",
        "associated_keywords": ["self-hosted LLM", "inference", "GPU", "privacy", "MLOps"],
        "description": (
            "Self-hosted LLMs on owned GPUs serving medical extraction and the underwriter assistant. "
            "Kept sensitive data in-house and raised throughput per GPU versus hosted APIs."
        ),
    },
    {
        "project_title": "LLM Provider Benchmarking (Price vs Accuracy)",
        "tech_stack": ["Python", "LLMs", "evaluation"],
        "outcomes_achieved": "Chose providers by price/accuracy for production extraction instead of a single vendor.",
        "associated_keywords": ["LLM eval", "benchmarking", "cost", "accuracy"],
        "description": (
            "Compared hosted and open-source LLM providers on extraction quality versus cost so production "
            "pipelines could switch or fall back without guessing."
        ),
    },
    {
        "project_title": "CI/CD Pipelines from Scratch (IngeniousZone)",
        "tech_stack": ["GitHub Actions", "Docker", "AWS", "IAM"],
        "outcomes_achieved": "Automated build/deploy across products with least-privilege IAM.",
        "associated_keywords": ["CI/CD", "DevOps", "GitHub Actions", "AWS"],
        "description": (
            "Part-time Software & AI Architect work: GitHub Actions, Dockerization, AWS console/IAM, and sprint "
            "delivery rails for AI products including Advisify and Aceprep."
        ),
    },
    {
        "project_title": "Real-time Firearms Detection",
        "tech_stack": ["PyTorch", "object detection", "computer vision"],
        "outcomes_achieved": "Real-time detection prototype from FAST NUCES research work.",
        "associated_keywords": ["YOLO", "object detection", "computer vision", "real-time"],
        "description": (
            "Computer-vision detection work originating as an HEC-funded research assistant project on "
            "real-time object detection."
        ),
    },
    {
        "project_title": "Advisify",
        "tech_stack": ["Python", "LLMs", "RAG"],
        "outcomes_achieved": "Immigration pathway suggestions from CV and preferences.",
        "associated_keywords": ["LLM", "CV parsing", "recommendations", "RAG"],
        "description": (
            "AI product direction for Advisify: map a candidate CV and preferences to immigration pathways."
        ),
    },
    {
        "project_title": "Aceprep",
        "tech_stack": ["Python", "RAG", "LLMs"],
        "outcomes_achieved": "Indexed O/A-level past papers with grounded Q&A and answer validation.",
        "associated_keywords": ["RAG", "education", "Q&A", "retrieval"],
        "description": (
            "Indexed past papers with grounded question answering and answer validation for exam prep."
        ),
    },
]


async def seed_agent_case_studies(db: Session) -> int:
    existing = {
        row.project_title.lower()
        for row in db.query(PortfolioItem).filter(PortfolioItem.origin == WorkOrigin.agent.value).all()
    }
    added = 0
    for study in CASE_STUDIES:
        if study["project_title"].lower() in existing:
            continue
        await update_portfolio_matrix(
            project_title=study["project_title"],
            tech_stack=study["tech_stack"],
            outcomes_achieved=study["outcomes_achieved"],
            associated_keywords=study["associated_keywords"],
            description=study["description"],
            kind="project",
            db=db,
        )
        added += 1
    return added
