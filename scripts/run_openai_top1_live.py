"""Run one real GitHub + OpenAI evidence/narrative test without DB or Telegram."""
from datetime import datetime, timedelta, timezone
import json
from collections import Counter
import argparse
import re
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.github_client import GitHubClient
from collectors.github_release import release_event
from collectors.github_repository import repository_model
from config.settings import Settings
from config import rules
from models.domain import Candidate, Event, Metric, ReportPeriod, Repository
from services.deep_analysis_service import DeepAnalysisCollector
from services.evidence_service import (LLM_EXTRACTION_KEYS, STRUCTURED_EVIDENCE_KEYS,
    normalize_source_type, structure_evidence, validate_extraction)
from services.narrative_service import NarrativeService, OpenAIJSONClient, extraction_payload


REPOSITORY = 'damianvtran/local-operator'
OUTPUT = Path('artifacts/openai-top1-test')
KST = timezone(timedelta(hours=9))
PERIOD = ReportPeriod(datetime(2026, 9, 27, 19, tzinfo=KST), datetime(2026, 10, 4, 19, tzinfo=KST))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def usage_total(usages):
    return {key: sum(item.get(key, 0) for item in usages)
            for key in ('input_tokens', 'output_tokens', 'total_tokens')}


def source_manifest_matches(saved):
    manifest = saved.get('source_manifest') or {}
    expected = {'schema_version':2, 'repository':REPOSITORY,
                'period_start':PERIOD.start.isoformat(), 'period_end':PERIOD.end.isoformat()}
    return all(manifest.get(key) == value for key, value in expected.items())


def evidence_counts(documents):
    counts = Counter(normalize_source_type(document.get('source_type') or document.get('kind'))
                     for document in documents)
    kinds = ('README','Release','Issue','IssueComment','Discussion','DiscussionComment',
             'PR','PRComment','Security','Other')
    return {key:counts[key] for key in kinds}


def compact_saved_documents(documents):
    priority = {'readme':0,'official_docs':0,'release':1,'security':1,
                'issues':2,'pulls':2,'discussions':2,'comment':3}
    compacted = []
    comment_groups = Counter()
    fingerprints = {}
    release_count = patch_release_count = 0
    for document in documents:
        kind = document['kind']
        source_type = normalize_source_type(document.get('source_type') or kind)
        limit = (rules.DEEP_COMMENT_CHAR_LIMIT if source_type.endswith('Comment') else
                 rules.DEEP_RELEASE_CHAR_LIMIT if source_type == 'Release' else
                 rules.DEEP_COMMUNITY_CHAR_LIMIT if kind in {'issues','pulls','discussions'} else
                 rules.DEEP_DOCUMENT_CHAR_LIMIT)
        item = dict(document)
        item['source_type'] = source_type
        item['truncated'] = bool(item.get('truncated')) or len(item['text']) > limit
        item['text'] = item['text'][:limit]
        if source_type == 'Release':
            release_text = (str(item.get('title') or '') + '\n' + item['text']).casefold()
            meaningful = (bool(re.search(r'\bbreaking\b|^#{1,6}\s*(?:new features|features added)',
                                         release_text, re.MULTILINE)) or
                          bool(re.search(r'\bv?(?:[1-9]\d*)\.0(?:\.0)?\b',
                                         str(item.get('title') or ''), re.IGNORECASE)))
            if not meaningful:
                patch_release_count += 1
                if patch_release_count > 1:
                    continue
            if release_count >= rules.DEEP_LIMITS['releases']:
                continue
            release_count += 1
        if source_type.endswith('Comment'):
            group = (source_type, re.sub(r'#.*$', '', item['url']))
            words = set(re.findall(r'[\w]+', item['text'].casefold()))
            if any(words and len(words & old) / max(1, len(words | old)) >= 0.72
                   for old in fingerprints.get(group, [])):
                continue
            if comment_groups[group] >= rules.DEEP_COMMENT_PER_PARENT:
                continue
            comment_groups[group] += 1
            fingerprints.setdefault(group, []).append(words)
        if not any(existing['url'] == item['url'] and existing['text'] == item['text'] for existing in compacted):
            compacted.append(item)
    bounded, used = [], 0
    for document in sorted(compacted, key=lambda item:priority.get(item['kind'], 9)):
        if used + len(document['text']) <= rules.DEEP_TOTAL_CHAR_LIMIT:
            bounded.append(document)
            used += len(document['text'])
    return bounded


def candidate_from_saved(saved):
    candidate = Candidate(Repository(**saved['repository']), {'manual_top1_validation'})
    candidate.deep_analysis_manifest = saved.get('source_manifest') or {}
    candidate.deep_analysis_diagnostics = (saved.get('validation_diagnostics') or {}).get('collection', {})
    candidate.changes_since_report = list(saved.get('changes_since_report') or [])
    candidate.metrics = {name:Metric(**metric) for name,metric in (saved.get('metrics') or {}).items()}
    for fact in saved.get('facts', []):
        if fact.get('type') != 'event' or not fact.get('occurred_at'):
            continue
        source = fact.get('source')
        if not source:
            source = 'release' if '/releases/' in fact.get('url','') else 'security' if '/advisories/' in fact.get('url','') else 'other'
        candidate.events.append(Event(fact.get('kind','event'), fact.get('text',''),
            fact.get('url',''), fact['occurred_at'], source))
    return candidate


def validate_saved_source(saved, documents, structured_path):
    candidate = candidate_from_saved(saved)
    evidence = structure_evidence(candidate, documents)
    previous = json.loads(structured_path.read_text(encoding='utf-8')) if structured_path.exists() else {}
    extraction = {}
    for key in LLM_EXTRACTION_KEYS:
        items = previous.get(key, [])
        if key == 'developer_impact':
            extraction[key] = [item for item in items if isinstance(item, dict)]
        else:
            extraction[key] = [item for item in items
                               if isinstance(item,dict) and item.get('quote') and item.get('source_id')]
    diagnostics = {}
    validated = validate_extraction(extraction, evidence, LLM_EXTRACTION_KEYS,
                                    strict=False, diagnostics=diagnostics)
    payload = extraction_payload(evidence)
    return evidence, validated, diagnostics, len(json.dumps(payload,ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--reuse-source', action='store_true',
                        help='Reuse previously collected real GitHub source evidence; call only OpenAI')
    parser.add_argument('--validate-source', action='store_true',
                        help='Validate saved source deterministically without GitHub, OpenAI, DB, or Telegram')
    args = parser.parse_args(argv)
    if args.validate_source:
        saved = json.loads((OUTPUT / 'source_evidence.json').read_text(encoding='utf-8'))
        documents = compact_saved_documents(saved['documents'])
        evidence, validated, diagnostics, chars = validate_saved_source(
            saved, documents, OUTPUT/'structured_evidence.json')
        counts = evidence_counts(documents)
        print(f'Evidence documents: {len(documents)}; types: {counts}')
        print(f'Representative releases: {counts["Release"]}')
        print(f'Representative comments: {sum(counts[key] for key in ("IssueComment","DiscussionComment","PRComment"))}')
        print('Deterministic project_summary: ' + json.dumps(evidence['project_summary'],ensure_ascii=False))
        print('Deterministic attention_signals: ' + json.dumps(evidence['attention_signals'],ensure_ascii=False))
        print('Validated LLM fields: ' + json.dumps({key:len(value) for key,value in validated.items()},ensure_ascii=False))
        print('Validation: ' + json.dumps(diagnostics,ensure_ascii=False))
        print(f'Estimated extraction input: {chars} chars (~{(chars+3)//4} tokens)')
        return 0
    settings = Settings.load('.env')
    if not settings.openai_api_key:
        raise ValueError('Missing settings: OPENAI_API_KEY')
    if not args.reuse_source and not settings.github_token:
        raise ValueError('Missing settings: GITHUB_TOKEN')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    github = None if args.reuse_source else GitHubClient(settings.github_token)
    llm = OpenAIJSONClient(settings.openai_api_key, settings.openai_model)
    try:
        if args.reuse_source:
            saved = json.loads((OUTPUT / 'source_evidence.json').read_text(encoding='utf-8'))
            manifest = saved.get('source_manifest') or {}
            if not source_manifest_matches(saved):
                raise ValueError('Saved source artifact is stale or lacks a matching source manifest; recollect it without --reuse-source')
            candidate = candidate_from_saved(saved)
            documents = compact_saved_documents(saved['documents'])
        else:
            candidate = Candidate(repository_model(github.repository(REPOSITORY)), {'manual_top1_validation'})
            for raw in github.releases_for_period(REPOSITORY, PERIOD.start, PERIOD.end):
                event = release_event(raw)
                if event:
                    candidate.events.append(event)
            documents = DeepAnalysisCollector(github).collect(candidate, PERIOD)
        candidate.deep_analysis_manifest = {
            **getattr(candidate, 'deep_analysis_manifest', {}),
            'schema_version':2, 'repository':candidate.repository.full_name,
            'period_start':PERIOD.start.isoformat(), 'period_end':PERIOD.end.isoformat(),
            'document_count':len(documents),
            'evidence_chars':sum(len(document['text']) for document in documents),
        }
        evidence = structure_evidence(candidate, documents)
        extraction_chars = len(json.dumps(extraction_payload(evidence),ensure_ascii=False))
        if not args.reuse_source:
            write_json(OUTPUT / 'source_evidence.json', evidence)
        NarrativeService(llm).generate(candidate)
        structured = {key:evidence.get(key, []) for key in STRUCTURED_EVIDENCE_KEYS}
        structured['evidence_references'] = evidence['important_links']
        structured['validation_diagnostics'] = evidence['validation_diagnostics']
        counts = evidence_counts(documents)
        community_documents = sum(counts[key] for key in (
            'Issue','IssueComment','Discussion','DiscussionComment','PR','PRComment'))
        empty_reasons = {}
        for key in STRUCTURED_EVIDENCE_KEYS:
            if structured[key]:
                continue
            if key in {'reaction','positive_opinions','concerns','debates'}:
                empty_reasons[key] = ('No Issue/Discussion/PR evidence collected' if community_documents == 0 else
                                      'Community documents were collected, but no supported item survived extraction validation')
            elif key == 'risks' and counts['Security'] == counts['Release'] == 0:
                empty_reasons[key] = 'No Release or Security document was collected; no supported risk extracted'
            else:
                empty_reasons[key] = 'No supported evidence survived extraction validation'
        succeeded = len(llm.usages) == 2 and not any(
            error.startswith('narrative degraded:') for error in candidate.errors)
        metadata = {
            'repository': candidate.repository.full_name,
            'period_start': PERIOD.start.isoformat(), 'period_end': PERIOD.end.isoformat(),
            'model': settings.openai_model, 'openai_calls': llm.call_count,
            'successful_openai_calls': len(llm.usages), 'openai_usage':usage_total(llm.usages),
            'usage_by_call':llm.usages, 'estimated_cost_usd':None,
            'document_count': len(documents), 'evidence_type_counts':counts,
            'representative_release_count':counts['Release'],
            'representative_comment_count':sum(counts[key] for key in (
                'IssueComment','DiscussionComment','PRComment')),
            'evidence_chars':sum(len(document['text']) for document in documents),
            'estimated_extraction_input_chars':extraction_chars,
            'estimated_extraction_input_tokens':(extraction_chars+3)//4,
            'truncated_documents':sum(bool(document['truncated']) for document in documents),
            'source_manifest':evidence['source_manifest'],
            'validation_diagnostics':evidence['validation_diagnostics'],
            'empty_field_reasons':empty_reasons, 'candidate_errors': candidate.errors,
            'github_usage': github.request_summary() if github else {'reused_saved_source':True},
            'telegram_called': False, 'database_used': False,
            'validation_status':'SUCCESS' if succeeded else 'FAILED',
        }
        if succeeded:
            write_json(OUTPUT / 'source_evidence.json', evidence)
            write_json(OUTPUT / 'structured_evidence.json', structured)
            write_json(OUTPUT / 'final_narrative.json', candidate.narrative)
            write_json(OUTPUT / 'run_metadata.json', metadata)
        else:
            write_json(OUTPUT / 'failed_run_metadata.json', metadata)
        print(f"Repository: {candidate.repository.full_name}")
        print(f"Evidence documents: {len(documents)}; types: {counts}")
        print(f"OpenAI calls: {llm.call_count}; model: {settings.openai_model}; usage: {usage_total(llm.usages)}")
        print('Structured evidence:')
        print(json.dumps(structured, ensure_ascii=False, indent=2))
        print('Narrative:')
        print(json.dumps(candidate.narrative, ensure_ascii=False, indent=2))
        print('Empty field reasons: ' + json.dumps(empty_reasons, ensure_ascii=False))
        quote_rejections = [item for item in
            evidence.get('validation_diagnostics',{}).get('llm_extraction',{}).get('items',[])
            if item.get('reason') == 'quote_not_found']
        print('Quote rejection diagnostics: ' + json.dumps(quote_rejections,ensure_ascii=False))
        if not succeeded:
            print('OpenAI validation: FAILED (no successful two-stage result)')
        return 0 if succeeded else 2
    finally:
        if github:
            github.close()
        llm.client.close()


if __name__ == '__main__':
    raise SystemExit(main())
