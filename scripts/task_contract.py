"""Versioned update and task receipt contracts; no filesystem or science imports."""
from copy import deepcopy
import re

COUNTERS = ('selected', 'ready_before', 'attempted', 'succeeded', 'failed',
            'interrupted', 'deferred', 'unvisited')
OUTCOMES = {'success', 'partial', 'failed', 'interrupted'}
STAGES = {'preflight', 'discovery', 'snapshot', 'download', 'alignment',
          'evidence', 'aggregation', 'delivery', 'finished'}
ERROR_STAGES = STAGES | {'cleanup', 'prior_sync', 'website_sync', 'worker', 'receipt'}
ERROR_CODES = {'EVIDENCE_INVALID','EVIDENCE_INCOMPATIBLE','EVIDENCE_ADMISSION_REQUIRED',
               'EVIDENCE_COHORT_INCOMPATIBLE','EVIDENCE_RECOVERY_REQUIRED','EVIDENCE_REBUILD_REQUIRED',
               'UPDATE_INTERRUPTED', 'UPDATE_FAILED', 'STAGE_FAILED', 'CLEANUP_FAILED',
               'DELIVERY_FAILED', 'CONFIG_CHANGED', 'INVALID_RESULT', 'BATCH_ID_MISMATCH',
               'EXIT_STATUS_MISMATCH', 'RECEIPT_MISSING', 'RECEIPT_INVALID', 'CHECKPOINT_WRITE_FAILED', 'DELIVERY_PLAN_MISMATCH', 'DELIVERY_SUPERSEDED', 'MISSING_RELEASE', 'DELIVERY_ONLY_REPAIR_REQUIRED'}
DELIVERY_STATES = {'succeeded', 'failed', 'skipped', 'pending', 'unknown', 'inconsistent', 'superseded'}
ALIASES = dict(processed_samples='attempted', ready_samples='ready_before',
               deferred_samples='deferred', selected_samples='selected')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')


class ContractError(ValueError):
    def __init__(self, code, message):
        super().__init__(code + ': ' + message)
        self.code = code


def error_record(code, stage, message, accession=None, retryable=None):
    return dict(code=code, stage=stage, accession=accession, message=str(message), error=str(message),
                retryable=retryable)


def phase_for(execution):
    if not isinstance(execution, dict):
        raise ContractError('INVALID_RESULT', 'execution must be an object')
    state, outcome = execution.get('state'), execution.get('outcome')
    if state in {'queued', 'running', 'unknown'} and outcome is None:
        return state
    if state == 'stopped' and outcome in OUTCOMES:
        return 'complete' if outcome == 'success' else outcome
    raise ContractError('INVALID_RESULT', 'invalid execution state/outcome')


def execution_from_result(value, returncode):
    outcome = value.get('outcome')
    allowed = {'success': {0}, 'partial': {1}, 'failed': {1}, 'interrupted': {130, 143}}
    if outcome not in allowed or type(returncode) is not int or returncode not in allowed[outcome]:
        raise ContractError('EXIT_STATUS_MISMATCH', 'result outcome disagrees with process exit')
    return dict(state='stopped', outcome=outcome)


def unknown_summary():
    return dict(scope='invocation', **dict.fromkeys(COUNTERS))


def _delivery(value):
    state = value.get('status')
    return {'up_to_date': 'succeeded', 'synchronized': 'succeeded',
            'not_default_reference': 'skipped', 'not_configured': 'skipped', 'planned': 'pending',
            'no_release': 'pending'}.get(state, state if state in DELIVERY_STATES else 'unknown')


def project_aggregations(groups, aggregations):
    projected, resources, websites = [], [], []
    for species, (reference, annotation) in sorted(groups.items()):
        value = aggregations.get(species, {})
        if not isinstance(value,dict):
            raise ContractError('INVALID_RESULT', 'aggregation must be an object')
        for field in ('status','release_id'):
            if value.get(field) is not None and not isinstance(value[field],str):
                raise ContractError('INVALID_RESULT', 'invalid aggregation state/release')
        for field in ('new_samples','threshold'):
            if value.get(field) is not None and (type(value[field]) is not int or value[field]<0):
                raise ContractError('INVALID_RESULT', 'invalid aggregation count')
        for field in ('resource_sync','website_sync'):
            sync=value.get(field)
            if sync is not None:
                if not isinstance(sync,dict):
                    raise ContractError('INVALID_RESULT', 'delivery must be an object')
                for key in ('status','release_id','snapshot_id'):
                    if sync.get(key) is not None and not isinstance(sync[key],str):
                        raise ContractError('INVALID_RESULT', 'invalid delivery identity/state')
        namespace = dict(species=species, reference_id=reference, annotation_id=annotation)
        projected.append(dict(namespace, aggregation_status=value.get('status'),
                              new_samples=value.get('new_samples'), threshold=value.get('threshold'),
                              publication_mode=value.get('publication_mode'),checkpoint_reached=value.get('checkpoint_reached'),
                              release_id=value.get('release_id')))
        resource = value.get('resource_sync')
        resources.append(dict(namespace, status=_delivery(resource) if resource else 'unknown',
                              release_id=(resource or {}).get('release_id', value.get('release_id'))))
        website = value.get('website_sync')
        if website is not None and website not in websites:
            websites.append(website)
    website = None
    if len(websites) == 1:
        website = dict(status=_delivery(websites[0]), snapshot_id=websites[0].get('snapshot_id'))
    elif websites:
        website = dict(status='inconsistent', snapshot_id=None)
    return projected, dict(resources=resources, website=website)


def build_update_result(context, outcome, summary, groups, delivery, errors):
    value = dict(schema_version=2, kind='update_result', batch_id=context.get('batch_id'),
                 invocation_id=context['invocation_id'], outcome=outcome,
                 stage=context.get('stage', 'preflight'), summary=deepcopy(summary or unknown_summary()),
                 groups=deepcopy(groups), delivery=deepcopy(delivery), errors=deepcopy(errors),
                 status='complete' if outcome == 'success' else outcome,
                 aggregations=deepcopy(context.get('aggregations', {})), snapshot=context.get('snapshot'))
    value.update({name: value['summary'][counter] for name, counter in ALIASES.items()})
    return validate_update_result(value, context.get('batch_id'))


def _validate_summary(summary):
    if not isinstance(summary, dict) or set(summary) != set(COUNTERS) | {'scope'} or summary['scope'] != 'invocation':
        raise ContractError('INVALID_RESULT', 'invalid summary fields/scope')
    values = [summary[k] for k in COUNTERS]
    if all(v is None for v in values):
        return
    if any(type(v) is not int or v < 0 for v in values):
        raise ContractError('INVALID_RESULT', 'counters must all be nonnegative integers or all unknown')
    s = summary
    if (s['selected'] != s['ready_before'] + s['attempted'] + s['deferred'] + s['unvisited']
            or s['attempted'] != s['succeeded'] + s['failed'] + s['interrupted']):
        raise ContractError('INVALID_RESULT', 'summary partitions disagree')


def validate_update_result(value, expected_batch_id):
    if not isinstance(value, dict):
        raise ContractError('INVALID_RESULT', 'update result must be an object')
    if 'schema_version' not in value:
        if 'batch_id' not in value:
            raise ContractError('BATCH_ID_MISMATCH', 'live update omitted batch identity')
        return normalize_legacy_result(value, expected_batch_id)
    if type(value['schema_version']) is not int or value['schema_version'] != 2:
        raise ContractError('INVALID_RESULT', 'unsupported update result version')
    required = {'kind', 'batch_id', 'invocation_id', 'outcome', 'stage', 'summary', 'groups',
                'delivery', 'errors', 'status', 'aggregations', 'snapshot'} | set(ALIASES)
    if not required <= set(value) or value['kind'] != 'update_result':
        raise ContractError('INVALID_RESULT', 'incomplete update result or incorrect kind')
    batch = value['batch_id']
    if batch != expected_batch_id or (batch is not None and (not isinstance(batch, str) or not IDENTIFIER.fullmatch(batch))):
        raise ContractError('BATCH_ID_MISMATCH', 'update returned an unexpected batch identity')
    if not isinstance(value['invocation_id'], str) or not IDENTIFIER.fullmatch(value['invocation_id']):
        raise ContractError('INVALID_RESULT', 'invalid invocation identity')
    if value['outcome'] not in OUTCOMES or value['stage'] not in STAGES:
        raise ContractError('INVALID_RESULT', 'invalid outcome/stage')
    if value['status'] != phase_for(dict(state='stopped', outcome=value['outcome'])):
        raise ContractError('INVALID_RESULT', 'legacy status disagrees with outcome')
    _validate_summary(value['summary'])
    if any(value[k] != value['summary'][v] or type(value[k]) is not type(value['summary'][v]) for k,v in ALIASES.items()):
        raise ContractError('INVALID_RESULT', 'legacy counts disagree with summary')
    if not isinstance(value['aggregations'], dict) or (value['snapshot'] is not None and not isinstance(value['snapshot'], str)):
        raise ContractError('INVALID_RESULT', 'invalid aggregation/snapshot structure')
    if not isinstance(value['groups'], list):
        raise ContractError('INVALID_RESULT', 'groups must be a list')
    for group in value['groups']:
        if not isinstance(group, dict) or not {'species','reference_id','annotation_id','aggregation_status','new_samples','threshold','release_id'} <= set(group):
            raise ContractError('INVALID_RESULT', 'invalid scientific group')
        for key in ('species','reference_id','annotation_id'):
            if not isinstance(group[key], str) or not group[key]:
                raise ContractError('INVALID_RESULT', 'invalid group namespace')
        for key in ('aggregation_status','release_id'):
            if group[key] is not None and not isinstance(group[key],str):
                raise ContractError('INVALID_RESULT', 'invalid group state/release')
        for key in ('new_samples','threshold'):
            if group[key] is not None and (type(group[key]) is not int or group[key] < 0):
                raise ContractError('INVALID_RESULT', 'invalid group count')
    validate_details(value['delivery'],value['errors'])

    return value



def validate_details(delivery,errors):
    if not isinstance(delivery, dict) or set(delivery) != {'resources', 'website'} or not isinstance(delivery['resources'], list):
        raise ContractError('INVALID_RESULT', 'invalid delivery structure')
    for entry in delivery['resources'] + ([] if delivery['website'] is None else [delivery['website']]):
        if not isinstance(entry, dict) or entry.get('status') not in DELIVERY_STATES:
            raise ContractError('INVALID_RESULT', 'invalid delivery state')
    for entry in delivery['resources']:
        if not {'species','reference_id','annotation_id','release_id'} <= set(entry):
            raise ContractError('INVALID_RESULT', 'incomplete resource delivery identity')
        for key in ('species','reference_id','annotation_id'):
            if not isinstance(entry[key],str) or not entry[key]:
                raise ContractError('INVALID_RESULT', 'invalid resource namespace')
        if entry['release_id'] is not None and not isinstance(entry['release_id'],str):
            raise ContractError('INVALID_RESULT', 'invalid resource release identity')
    website=delivery['website']
    if website is not None and ('snapshot_id' not in website or (website['snapshot_id'] is not None and not isinstance(website['snapshot_id'],str))):
        raise ContractError('INVALID_RESULT', 'invalid website snapshot identity')
    if not isinstance(errors, list):
        raise ContractError('INVALID_RESULT', 'errors must be a list')
    for error in errors:
        if (not isinstance(error, dict) or not {'code','stage','message','accession','retryable'} <= set(error)
                or error['code'] not in ERROR_CODES or error['stage'] not in ERROR_STAGES
                or not isinstance(error['message'], str)
                or (error['accession'] is not None and not isinstance(error['accession'], str))
                or (error['retryable'] is not None and type(error['retryable']) is not bool)):
            raise ContractError('INVALID_RESULT', 'invalid structured error')


def validate_delivery_result(value,expected_job_id):
    required={'schema_version','kind','job_id','invocation_id','outcome','status','delivery','errors'}
    if not isinstance(value,dict) or not required<=set(value) or type(value['schema_version'])is not int or value['schema_version']!=2 or value['kind']!='delivery_result':
        raise ContractError('INVALID_RESULT','invalid delivery result')
    job=value['job_id']
    if job!=expected_job_id or (job is not None and (not isinstance(job,str)or not re.fullmatch('dj-[a-f0-9]{40}',job))):
        raise ContractError('DELIVERY_PLAN_MISMATCH','delivery job identity mismatch')
    if not isinstance(value['invocation_id'],str)or not IDENTIFIER.fullmatch(value['invocation_id']):
        raise ContractError('INVALID_RESULT','invalid delivery invocation identity')
    if value['outcome']not in OUTCOMES or value['status']!=phase_for(dict(state='stopped',outcome=value['outcome'])):
        raise ContractError('INVALID_RESULT','invalid delivery outcome')
    if {'summary','processed_samples','selected_samples'}&set(value):
        raise ContractError('INVALID_RESULT','delivery cannot carry scientific sample counters')
    validate_details(value['delivery'],value['errors'])
    return value


def validate_result(value,operation,identity):
    if operation=='delivery':return validate_delivery_result(value,identity)
    if operation=='update':return validate_update_result(value,identity)
    raise ContractError('INVALID_RESULT','unknown task operation')


def normalize_legacy_result(value, expected_batch_id):
    """Read-only projection. Missing identity is allowed only for stored receipts."""
    if not isinstance(value, dict) or 'schema_version' in value or value.get('status') not in {'complete','partial','failed','interrupted'}:
        raise ContractError('INVALID_RESULT', 'unsupported legacy result')
    if value.get('batch_id', expected_batch_id) != expected_batch_id:
        raise ContractError('BATCH_ID_MISMATCH', 'historical result identity disagrees with task')
    result = deepcopy(value)
    result['batch_id'] = expected_batch_id
    result['identity_source'] = 'result' if 'batch_id' in value else 'task_record'
    result['outcome'] = 'success' if value['status'] == 'complete' else value['status']
    result['summary'] = unknown_summary()
    for name,counter in ALIASES.items():
        count = value.get(name)
        if count is not None and (type(count) is not int or count < 0):
            raise ContractError('INVALID_RESULT', 'invalid legacy counter')
        result['summary'][counter] = count
    return result


def normalize_task_receipt(value, task_id, batch_id, attempt, operation='update', job_id=None):
    """Validate a stored receipt bound by its trusted task and attempt path."""
    if not isinstance(value, dict):
        raise ContractError('RECEIPT_INVALID', 'receipt must be an object')
    result = deepcopy(value)
    modern = 'schema_version' in value
    if modern:
        if (type(value['schema_version']) is not int or value['schema_version'] != 2
                or value.get('kind') != 'task_receipt' or value.get('task_id') != task_id
                or value.get('batch_id') != batch_id or value.get('operation','update')!=operation
                or (operation=='delivery' and value.get('job_id')!=job_id) or type(value.get('attempt')) is not int
                or value['attempt'] != attempt):
            raise ContractError('RECEIPT_INVALID', 'receipt version, kind or task identity mismatch')
        execution = value.get('execution', {})
        if phase_for(execution) != value.get('phase') or execution['state'] != 'stopped':
            raise ContractError('RECEIPT_INVALID', 'receipt is not a consistent terminal execution')
        if type(value.get('returncode')) is not int or type(value.get('result_replayed')) is not bool:
            raise ContractError('RECEIPT_INVALID', 'invalid receipt exit/replay fields')
        if not isinstance(value.get('errors'), list):
            raise ContractError('RECEIPT_INVALID', 'receipt errors must be a list')
        for error in value['errors']:
            if (not isinstance(error, dict) or error.get('code') not in ERROR_CODES
                    or not isinstance(error.get('message'), str)):
                raise ContractError('RECEIPT_INVALID', 'invalid receipt error')
    else:
        if operation!='update':raise ContractError('RECEIPT_INVALID','delivery requires a versioned receipt')
        phase = value.get('phase')
        if phase not in {'complete', 'partial', 'failed', 'interrupted'}:
            raise ContractError('RECEIPT_INVALID', 'unknown historical terminal phase')
        execution = dict(state='stopped', outcome='success' if phase == 'complete' else phase)
        result['errors'] = []
        result['result_replayed'] = None
    science = value.get('result')
    if science is not None:
        science = (validate_result(science, operation, job_id if operation=='delivery'else batch_id) if 'schema_version' in science
                   else normalize_legacy_result(science, batch_id))
        if modern:
            try:
                expected = execution_from_result(science, value['returncode'])
            except ContractError:
                if execution['outcome'] != 'failed' or not any(e['code'] == 'EXIT_STATUS_MISMATCH' for e in value['errors']):
                    raise ContractError('RECEIPT_INVALID', 'unreported process/result conflict')
            else:
                if expected != execution:
                    raise ContractError('RECEIPT_INVALID', 'receipt and scientific outcome disagree')
    elif modern and (execution['outcome'] not in {'failed', 'interrupted'} or not value['errors']):
        raise ContractError('RECEIPT_INVALID', 'terminal result is missing without failure diagnostics')
    if not modern and execution['outcome']=='success' and (science is None or type(value.get('returncode')) is not int or value['returncode']!=0):
        raise ContractError('RECEIPT_INVALID', 'historical success lacks verified science or zero exit')
    if not modern and science is not None:
        if science['outcome'] != execution['outcome']:
            raise ContractError('RECEIPT_INVALID', 'historical scientific and wrapper outcomes disagree')
        # Historical workers could record interrupted with a non-signal exit;
        # preserve that diagnosis, but never accept a nonzero successful exit.
        if execution['outcome'] == 'success' and value.get('returncode') != 0:
            raise ContractError('RECEIPT_INVALID', 'historical success has nonzero exit')
    result.update(execution=execution, phase=phase_for(execution), result=science)
    return result
