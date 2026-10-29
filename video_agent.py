"""Two-revision evidence-based video editing CLI. Only validated timelines reach FFmpeg."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from pydantic import BaseModel, ConfigDict, StrictInt, Field
from agent_core import AgentLoop, EventLogger, ModelClient, load_config, sha256_file
import media
import timeline
import renderer
import verify as movie_verify

FIELDS = ('duration_ms', 'width', 'height', 'fps', 'container', 'video_codec', 'audio_codec', 'clip_order', 'title_text', 'title_start_ms', 'title_end_ms', 'ending_text', 'ending_start_ms', 'ending_end_ms', 'transition_type', 'transition_ms', 'video_gain_db', 'video_mute', 'bgm_gain_db', 'bgm_fade_in_ms', 'bgm_fade_out_ms', 'fit')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def parse_response(text):
    return json.loads(text.strip().removeprefix('```json').removesuffix('```').strip())


def verify_sources(assets):
    for a in assets['assets']:
        path = Path(assets['asset_root']) / a['relative_path']
        if not path.is_file() or sha256_file(path) != a['sha256']:
            raise ValueError(f'Source asset changed; frozen analysis invalid: {a["relative_path"]}')


def ensure_provider_checkpoints(run_dir, config):
    """Never resume/commingle planning checkpoints created under another provider."""
    expected = {'model': config.model, 'api_base': config.api_base, 'credential_env': config.api_key_env}
    for checkpoint in sorted(Path(run_dir).glob('agent_v*/checkpoint.json')):
        state = read(checkpoint)
        if state.get('runtime_config') != expected:
            raise ValueError(f'{checkpoint} was created by a legacy/different provider; use a new run directory after migration')


def completed_matches(run_dir, version, raw, assets):
    req = read(run_dir / f'requirements_v{version}.json')
    if req['raw_text'] != raw:
        raise ValueError('Completed version belongs to a different request')
    verify_sources(assets)
    t, derived = timeline.check(read(run_dir / f'timeline_v{version}.json'), assets, req)
    stored = read(run_dir / f'derived_v{version}.json')
    if derived != stored or t.asset_manifest_hash != assets['manifest_hash']:
        raise ValueError('Completed timeline/derived result changed')
    validation = read(run_dir / f'validation_v{version}.json')
    if validation['status'] != 'pass':
        raise ValueError('Completed version has no passing validation')
    output = run_dir / f'output_v{version}.mp4'
    recorded = next((x['sha256'] for x in read(run_dir / 'manifest.json')['files']
                     if x['path'] == output.name), None)
    if recorded is None or sha256_file(output) != recorded:
        raise ValueError('Completed video differs from published manifest')


def requirements(run_dir, version, raw, client):
    path = run_dir / f'requirements_v{version}.json'
    if path.exists():
        previous = read(path)
        if raw != previous['raw_text']:
            raise ValueError('Frozen requirement baseline cannot be changed')
        return previous
    parent = read(run_dir / 'requirements_v1.json') if version == 2 else None
    instruction = ('Return JSON ONLY. Supported constraint fields: ' + ', '.join(FIELDS) + '. '
                   'Extract ONLY constraints explicitly requested in this text, never fill every field or invent captions/music. '
                   'Use milliseconds, gain dB numbers, codec libx264 for H.264, mp4, AAC, clip_order list of video/image. '
                   'For derived times explicitly requested as "last N seconds", use explicitly specified total duration if available; otherwise report needs_review rather than inventing a time. '
                   'Every constraint needs an exact verbatim substring of THIS request as source_text; include all unsupported or ambiguous requirements in unsupported_requirements with original wording. '
                   'Selecting source intervals, providing assets and mixing a supplied BGM are operational instructions supported by the renderer, not unsupported fields. '
                   'If automatic dialogue transcription is requested, set dialogue_transcription_requested true. ')
    if version == 1:
        prompt = instruction + ('Format: {"hard_constraints":[{"field":"...","expected":...,"source_text":"..."}],'
                                '"soft_preferences":[],"dialogue_transcription_requested":false,"unsupported_requirements":[]}. '
                                'Unrequested fields use declared output defaults during planning (not frozen hard requirements).\nREQUEST:\n' + raw)
        parsed = parse_response(client.respond([{'role': 'user', 'content': prompt}], purpose='extract_frozen_requirements').output_text)
        rows = parsed['hard_constraints']
        superseded = []
        hard = [{'requirement_id': f'R1_{r["field"]}', 'field': r['field'], 'expected': r['expected'], 'source_text': r['source_text']} for r in rows]
    else:
        prompt = instruction + ('Format: {"changes":[{"field":"...","expected":...,"source_text":"...","reason":"why explicitly added or replaced"}],'
                                '"soft_preferences":[],"dialogue_transcription_requested":false,"unsupported_requirements":[]}. '
                                'Return only explicit new/different constraints; statements keeping old values are NOT changes. '
                                'Any supported field may be added or replaced. Preserve all other baseline constraints.\nBASELINE:\n'
                                + json.dumps(parent['hard_constraints'], ensure_ascii=False) + '\nNEW REQUEST:\n' + raw)
        parsed = parse_response(client.respond([{'role': 'user', 'content': prompt}], purpose='extract_revision_changes').output_text)
        changes = parsed['changes']
        old_by_field = {r['field']: r for r in parent['hard_constraints']}
        hard = [dict(r) for r in parent['hard_constraints'] if r['field'] not in {x['field'] for x in changes}]
        superseded = []
        for change in changes:
            if not isinstance(change.get('reason'), str) or not change['reason'].strip():
                raise ValueError('Each revision change requires a reason')
            old = old_by_field.get(change['field'])
            if old:
                if old['expected'] == change['expected']:
                    raise ValueError(f'Unchanged constraint is not a revision: {change["field"]}')
                superseded.append({'requirement_id': old['requirement_id'], 'field': change['field'], 'reason': change['reason'], 'source_text': change['source_text']})
            hard.append({'requirement_id': f'R2_{change["field"]}', 'field': change['field'], 'expected': change['expected'], 'source_text': change['source_text'], 'reason': change['reason']})
    extracted = rows if version == 1 else changes
    fields = [r['field'] for r in extracted]
    if len(fields) != len(set(fields)) or any(f not in FIELDS for f in fields):
        raise ValueError(f'Duplicate or unsupported constraint fields need review: {fields}')
    for r in hard:
        source = r['source_text']
        if not isinstance(source, str) or not source or source not in (raw if r['requirement_id'].startswith('R2_') or version == 1 else parent['raw_text']):
            raise ValueError(f'Citation missing from original request: {r["field"]} {source!r}')
    if parsed.get('unsupported_requirements'):
        raise ValueError('Formal unsupported requirements need review: ' + str(parsed['unsupported_requirements']))
    if parsed.get('dialogue_transcription_requested'):
        # ASR is not a language model. This demonstration has no dialogue/transcription request.
        raise ValueError('Timed real ASR is required for dialogue/automatic captions; no configured timed ASR service')
    result = {'version': version, 'request_id': f'request_v{version}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}', 'raw_text': raw,
              'parent_request_id': parent['request_id'] if parent else None, 'request_ids': (parent['request_ids'] if parent else []) + [f'request_v{version}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}'],
              'hard_constraints': hard, 'soft_preferences': parsed.get('soft_preferences', []), 'superseded_requirement_ids': [x['requirement_id'] for x in superseded],
              'supersession_reasons': superseded, 'acceptance': {'status': 'frozen', 'automatic_fields': list(FIELDS), 'dialogue': 'none in synthetic demo', 'unsupported_requirements': []}}
    result['constraint_set_id'] = hashlib.sha256(json.dumps(hard, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    save(path, result)
    return result


class InspectArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    asset_id: str
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    interval_ms: StrictInt = Field(ge=100, le=5000)


class SubmitArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    timeline: dict


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


def spec(name, props, required=None):
    return {'type': 'function', 'name': name, 'description': {'submit_timeline': 'Validate proposed timeline against strict schema, actual sources and frozen hard requirements. Returns real errors.', 'inspect_interval': 'Dense sample true video frames and ask vision model what is visible.', 'render': 'Render ONLY validated timeline and return actual FFmpeg errors.', 'verify': 'Probe and fully decode pending movie, then inspect actual movie frames via vision model.', 'finish': 'Publish only when every hard constraint and movie validation passes.'}[name], 'parameters': {'type': 'object', 'properties': props, 'required': required or [], 'additionalProperties': False}}


def analyze(run_dir, inventory, client, config):
    path = run_dir / 'analysis.json'
    previous = read(path) if path.exists() else {'assets': []}
    cached = {row['asset_id']: row for row in previous['assets']}
    rows = []
    interval = media.sampling_interval(config)
    sampling = {'interval_ms': interval, 'frame_width': 640, 'endpoint_offset_ms': 200,
                'cache_version': 2, 'description': 'point samples across full video; unseen intervals unconfirmed'}
    for a in inventory['assets']:
        key = hashlib.sha256(json.dumps({'sha256': a['sha256'], 'sampling': sampling,
                                         'points': media.sample_points(a, config)}, sort_keys=True).encode()).hexdigest()
        if a['asset_id'] in cached and cached[a['asset_id']].get('cache_key') == key:
            rows.append(cached[a['asset_id']])
        else:
            obs = media.observe(a, inventory, run_dir, config, client)
            obs['cache_key'] = key
            rows.append(obs)
            save(path, {**previous, 'assets': rows + [cached[x['asset_id']] for x in inventory['assets'] if x['asset_id'] in cached and x['asset_id'] not in {z['asset_id'] for z in rows}]})
    result = {**previous, 'assets': rows, 'sampling': sampling, 'asset_manifest_hash': inventory['manifest_hash']}
    save(path, result)
    return result


def manifest(run_dir, inventory):
    files = []
    for p in sorted(run_dir.rglob('*')):
        if p.is_file() and p.name != 'manifest.json':
            files.append({'path': p.relative_to(run_dir).as_posix(), 'sha256': sha256_file(p), 'source_asset_manifest_hash': inventory['manifest_hash']})
    save(run_dir / 'manifest.json', {'files': files, 'source_files': [{'path': a['relative_path'], 'sha256': a['sha256']} for a in inventory['assets']], 'asset_manifest_hash': inventory['manifest_hash']})


def run(version, args):
    config = load_config(args.config)
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    request = Path(args.request).read_text(encoding='utf-8').strip()
    logger = EventLogger(run_dir, 'video', request_id=f'request_v{version}', revision=f'v{version}')
    started = time.monotonic()
    deadline = started + config.total_timeout_s
    try:
        output = run_dir / f'output_v{version}.mp4'
        if output.exists():
            assets = read(run_dir / 'assets.json')
            if version == 1 and Path(args.assets).resolve() != Path(assets['asset_root']).resolve():
                raise ValueError('Completed version belongs to different source assets')
            completed_matches(run_dir, version, request, assets)
            logger.event('already_completed', output=str(output), verified_request_and_sources=True)
            return
        ensure_provider_checkpoints(run_dir, config)
        client = ModelClient(config, logger)
        model_calls = [0]
        def bounded_model(model):
            original = model.respond
            def respond(*items, **options):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or model_calls[0] >= config.max_turns:
                    raise TimeoutError('Total video model-call/time budget exhausted')
                model_calls[0] += 1
                options['timeout'] = min(remaining, options.get('timeout', config.request_timeout_s))
                return original(*items, **options)
            model.respond = respond
        bounded_model(client)
        if version == 1:
            assets_path = run_dir / 'assets.json'
            assets = read(assets_path) if assets_path.exists() else media.inventory(args.assets, config)
            if Path(args.assets).resolve() != Path(assets['asset_root']).resolve():
                raise ValueError('Cached assets belong to another source directory')
            verify_sources(assets)
            if not assets_path.exists():
                save(assets_path, assets)
            analyze(run_dir, assets, client, config)
        else:
            if not (run_dir / 'output_v1.mp4').exists() or read(run_dir / 'validation_v1.json')['status'] != 'pass':
                raise ValueError('Revision requires a published passing v1')
            assets = read(run_dir / 'assets.json')
            verify_sources(assets)
            analyze(run_dir, assets, client, config)
        req = requirements(run_dir, version, request, client)
        loop_dir = run_dir / f'agent_v{version}'
        retry_number = 0
        while (loop_dir / 'checkpoint.json').exists():
            retry_number += 1
            loop_dir = run_dir / f'agent_v{version}_retry{retry_number}'
        loop_logger = EventLogger(loop_dir, 'video_planning', req['request_id'], f'v{version}')
        logger.event('planning_attempt', path=str(loop_dir), prior_attempt_preserved=retry_number > 0)
        config.max_turns = min(config.max_turns, 20)
        schema = timeline.Timeline.model_json_schema()
        # JSON Schema $defs must be at the root for local $ref resolution.
        submit_schema = spec('submit_timeline', {'timeline': {'$ref': '#/$defs/Timeline'}}, ['timeline'])
        submit_schema['parameters']['$defs'] = {**schema.get('$defs', {}), 'Timeline': {k: v for k, v in schema.items() if k != '$defs'}}
        loop = AgentLoop(config, loop_logger, [submit_schema, spec('inspect_interval', {'asset_id': {'type': 'string'}, 'start_ms': {'type': 'integer'}, 'end_ms': {'type': 'integer'}, 'interval_ms': {'type': 'integer'}}, ['asset_id', 'start_ms', 'end_ms', 'interval_ms']), spec('render', {}), spec('verify', {}), spec('finish', {})], {'finish': lambda: {'ok': False}})
        bounded_model(loop.model)
        # AgentLoop measures its own elapsed time; subtract preflight/analysis/extraction
        # so all stages share the Config total timeout, not a fresh per-stage allowance.
        config.total_timeout_s = max(0.001, config.total_timeout_s - (time.monotonic() - started))
        state = {'timeline': None, 'derived': None, 'pending': None, 'validation': None, 'render_attempts': 0}
        def submit_timeline(**kw):
            candidate = SubmitArgs.model_validate(kw).timeline
            save(run_dir / f'candidate_v{version}_{time.time_ns()}.json', candidate)
            t, derived = timeline.check(candidate, assets, req)
            if state['pending']:
                raise ValueError('Cannot change timeline after render; use fresh attempt/checkpoint')
            state.update(timeline=t, derived=derived)
            save(run_dir / f'timeline_v{version}.json', t.model_dump())
            save(run_dir / f'derived_v{version}.json', derived)
            return {'ok': True, 'derived': derived}
        def inspect_interval(**kw):
            x = InspectArgs.model_validate(kw)
            a = next(a for a in assets['assets'] if a['asset_id'] == x.asset_id)
            if a['kind'] != 'video' or x.end_ms > a['duration_ms'] or x.end_ms <= x.start_ms:
                raise ValueError('Inspection range must be inside video source')
            points = list(range(x.start_ms, x.end_ms, x.interval_ms)) + [x.end_ms-1]
            observation = media.observe(a, assets, run_dir, config, loop.model, points=sorted(set(points)), prefix=f'inspect_{time.time_ns()}')
            save(run_dir / f'inspection_v{version}_{time.time_ns()}.json', observation)
            return observation
        def render(**kw):
            EmptyArgs.model_validate(kw)
            if state['timeline'] is None:
                raise ValueError('submit_timeline first')
            if state['render_attempts'] >= 1 + config.repair_rounds:
                raise ValueError('Render repair budget exhausted')
            state['render_attempts'] += 1
            logger.event('render_attempt', attempt=state['render_attempts'])
            try:
                state['pending'] = renderer.render(state['timeline'], state['derived'], assets, config, run_dir)
                return {'ok': True, 'pending': state['pending'], 'attempt': state['render_attempts']}
            except Exception as exc:
                logger.event('render_failure', attempt=state['render_attempts'], error=str(exc))
                raise
        def verify(**kw):
            EmptyArgs.model_validate(kw)
            if not state['pending']:
                raise ValueError('Render a candidate before verification')
            report = movie_verify.verify(state['pending'], state['timeline'], state['derived'], assets, req, config, run_dir, loop.model)
            state['validation'] = report
            save(run_dir / f'validation_v{version}.json', report)
            return {'ok': report['status'] == 'pass', 'status': report['status'], 'failed_checks': [c for c in report['checks'] if c['status'] != 'pass'], 'sampled_frames': report['sampled_frames']}
        def finish(**kw):
            EmptyArgs.model_validate(kw)
            if state['timeline'] is None or not state['pending'] or not state['validation'] or state['validation']['status'] != 'pass':
                raise ValueError('Cannot finish before valid timeline, actual render and passing movie verification')
            timeline.check(state['timeline'].model_dump(), assets, req)
            if output.exists():
                raise ValueError('Already published output cannot be overwritten')
            Path(state['pending']).replace(output)
            logger.event('published', path=str(output), sha256=sha256_file(output))
            return {'ok': True, 'accepted': True, 'output': str(output), 'validation': f'validation_v{version}.json'}
        loop.handlers = {'submit_timeline': submit_timeline, 'inspect_interval': inspect_interval, 'render': render, 'verify': verify, 'finish': finish}
        task = ('Plan, validate, render, verify, then finish version ' + str(version) + '. Full timeline JSON Schema: ' + json.dumps(schema, ensure_ascii=False) + '. Unrequested output fields default to config.output=' + json.dumps(config.output) + ', container=mp4, video_codec=libx264, audio_codec=aac; do not invent captions or BGM unless requested. Frozen requirements: ' + json.dumps(req, ensure_ascii=False) + '\nAssets: ' + json.dumps(assets, ensure_ascii=False) + '\nObservations: ' + json.dumps(read(run_dir/'analysis.json'), ensure_ascii=False) + '\nVersion 1 example feasible math: 7000ms video + 3500ms image - 500ms dissolve = 10000ms. Version 2 can use image 3500ms then video 7000ms; choose actual content based on evidence. These durations are suggestions, NOT superseding request. For each clip provide all nullable fields; final transition cut 0. BGM from audio asset. All captions are provided copy, not ASR. Submit full JSON timeline with schema_version 1.0, version, parent_version, request_ids, asset_manifest_hash, constraint_set_id, output, clips, captions, bgm. Use tools; text-only answer does not finish.')
        system = f'You are the video editing planner on {config.model}/{config.reasoning_effort}. Use only observed evidence and frozen hard constraints. Do not invent visuals or bypass validation. Tools provide real errors for repair. Call submit_timeline, render, verify, finish, in order; inspect_interval when uncertain. Never claim pass on needs_review.'
        result = loop.run(task, system_prompt=system)
        logger.event('planning_result', result=result)
        top_usage = read(run_dir / 'usage.json')
        child_usage = read(loop_dir / 'usage.json')
        save(run_dir / 'usage.json', {'model': config.model, 'pricing': config.pricing, 'calls': top_usage['calls'] + child_usage['calls'], 'totals': {key: (sum((c.get(key) or 0) for c in top_usage['calls'] + child_usage['calls'] if c['status'] == 'succeeded') if key != 'estimated_cost_usd' else None) for key in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'estimated_cost_usd')}, 'cost_status': 'unknown'})
        if not result['ok']:
            raise RuntimeError(f'AgentLoop failed: {result}')
        if version == 2:
            one, two = read(run_dir/'timeline_v1.json'), read(run_dir/'timeline_v2.json')
            save(run_dir/'revision_diff.json', {'parent_version': 1, 'version': 2, 'superseded': req['supersession_reasons'], 'preserved_hard_requirement_ids': [r['requirement_id'] for r in req['hard_constraints'] if r['requirement_id'].startswith('R1_')], 'clip_order_before': [c['kind'] for c in one['clips']], 'clip_order_after': [c['kind'] for c in two['clips']], 'captions_before': one['captions'], 'captions_after': two['captions']})
        logger.event('finished', elapsed_s=time.monotonic()-started)
        manifest(run_dir, assets)
    except Exception as exc:
        logger.event('pipeline_failed', error_type=type(exc).__name__, error=str(exc))
        save(run_dir / f'failure_v{version}_{time.time_ns()}.json', {'status': 'failed', 'reason': str(exc), 'elapsed_s': time.monotonic()-started, 'checkpoint': str(locals().get('loop_dir', run_dir / f'agent_v{version}'))})
        raise


def fault_demo(run_dir, config):
    """Explicitly labeled diagnostic: real encoder failure, then real corrected encode."""
    target = Path(run_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    log = EventLogger(target, 'video_fault_injection', revision='diagnostic')
    base = [config.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30:d=0.5', '-frames:v', '15']
    bad = base + ['-c:v', 'deliberately_invalid_encoder', str(target / 'fault_invalid.pending.mp4')]
    log.event('fault_injection_started', source='explicit diagnostic, not an ordinary render failure', argv=bad)
    try:
        renderer.execute(bad, target, 'fault_injected_bad_encoder', config.command_timeout_s)
    except RuntimeError as exc:
        log.event('fault_injection_observed', status='failed_as_expected', reason=str(exc))
    else:
        raise RuntimeError('Fault injection did not fail: diagnostic cannot pass')
    good = base + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(target / 'fault_recovered.mp4')]
    renderer.execute(good, target, 'fault_recovered_encoder', config.command_timeout_s)
    info = media.probe(target / 'fault_recovered.mp4', config)
    if not any(s.get('codec_name') == 'h264' for s in info['streams']):
        raise ValueError('Recovered output does not decode as H.264')
    log.event('fault_recovered', status='pass', output=str(target / 'fault_recovered.mp4'), sha256=sha256_file(target / 'fault_recovered.mp4'))
    manifest(target, read(target / 'assets.json'))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--assets', required=True)
    revise = sub.add_parser('revise')
    revise.add_argument('--base-revision', type=int, required=True)
    diagnostic = sub.add_parser('fault-demo', help='Explicit labeled encoder-failure and recovery evidence')
    diagnostic.add_argument('--run-dir', required=True)
    diagnostic.add_argument('--config', default='config.example.json')
    for p in (create, revise):
        p.add_argument('--run-dir', required=True)
        p.add_argument('--request', required=True)
        p.add_argument('--config', default='config.example.json')
    args = parser.parse_args()
    if args.command == 'revise' and args.base_revision != 1:
        parser.error('Only revision 1 can be revised to version 2')
    try:
        if args.command == 'fault-demo':
            fault_demo(args.run_dir, load_config(args.config))
        else:
            run(1 if args.command == 'create' else 2, args)
    except Exception as exc:
        print(f'VIDEO_AGENT_FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
