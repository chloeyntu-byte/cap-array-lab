"""Bounded handling of a captured Maestro runtime and owned setup copies.

This module never changes the invoking session or submits a job.
"""
import copy
import json
import hashlib
import re
import math
import os
import shutil
import xml.etree.ElementTree as ET

POLICY_FIELDS = frozenset(('command', 'distributionmethod', 'interfacename', 'maxjobs',
                           'starttimeout', 'configuretimeout', 'runtimeout', 'lingertimeout'))
RUN_FIELDS = frozenset(('run_mode', 'RunIn', 'DivideJobs', 'JobLimit'))
STORAGE_FIELDS = frozenset(('project_dir', 'save_res_dir', 'save_dir', 'use_local_psf_dir',
                            'local_psf_dir', 'use_local_rdb_dir', 'local_rdb_dir', 'fallback'))
FIELDS = POLICY_FIELDS | RUN_FIELDS | STORAGE_FIELDS | frozenset(('env_options',))
NATIVE_POLICY_NAMES = {'command': 'jobsubmitcommand'}


def normalize_overrides(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('Runtime overrides must be an object')
    unknown = set(value) - FIELDS
    if unknown:
        raise ValueError('Unsupported runtime override: ' + ', '.join(sorted(unknown)))
    result = copy.deepcopy(value)
    for key, val in result.items():
        if key == 'env_options':
            if not isinstance(val, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or k in ('modelFiles', 'controlMode') for k in val):
                raise ValueError('env_options must be an object and cannot override modelFiles or controlMode')
            def valid(v):
                return v is None or isinstance(v, (bool, str, int)) or (isinstance(v, float) and math.isfinite(v)) or (isinstance(v, list) and all(valid(x) for x in v))
            if not all(valid(v) for v in val.values()):
                raise ValueError('Unsupported env_options value')
        elif key == 'JobLimit':
            if not isinstance(val, list) or any(not isinstance(row, (list, tuple)) or len(row) != 2 or
                    not isinstance(row[0], str) or not isinstance(row[1], (str, int)) or
                    isinstance(row[1], bool) for row in val):
                raise ValueError('JobLimit must contain test name and limit pairs')
        elif key in ('use_local_psf_dir', 'use_local_rdb_dir'):
            if not isinstance(val, bool):
                raise ValueError(key + ' must be true or false')
        elif not isinstance(val, (str, int)) or isinstance(val, bool):
            raise ValueError(key + ' must be text or an integer')
        if isinstance(val, str) and (len(val) > 16384 or any(ord(c) < 32 for c in val)):
            raise ValueError('Invalid characters or length in ' + key)
        if key == 'maxjobs':
            try:
                if int(val) < 1 or str(int(val)) != str(val):
                    raise ValueError()
            except ValueError:
                raise ValueError('maxjobs must be a positive integer')
        if key in ('starttimeout', 'configuretimeout', 'runtimeout', 'lingertimeout'):
            if isinstance(val, str) and not val.strip():
                result[key] = '-1'
            if str(val).strip() not in ('', '-1'):
                try:
                    if int(val) < 1 or str(int(val)) != str(val):
                        raise ValueError()
                except ValueError:
                    raise ValueError(key + ' must be positive seconds, -1, or blank')
    return result


def selected_test(runtime):
    source = (runtime or {}).get('source') or {}
    tests = source.get('tests') or []
    selected = (runtime or {}).get('source_test')
    names = [t.get('name') for t in tests if isinstance(t, dict) and t.get('name')]
    if selected and selected not in names:
        raise ValueError('Selected source test is not in the invoking Maestro')
    if not selected and len(names) == 1:
        selected = names[0]
    if not selected and len(names) > 1:
        raise ValueError('Select the source Maestro test explicitly')
    return selected


def effective_runtime(runtime):
    """Return a preview, explicitly distinct from native readback."""
    runtime = runtime or {}
    overrides = normalize_overrides(runtime.get('overrides'))
    snapshot = runtime.get('snapshot') or {}
    test = selected_test(runtime)
    row = next((x for x in snapshot.get('tests', []) if x.get('name') == test), {})
    policy = row if row.get('available') else snapshot.get('global') or {}
    props = policy.get('properties') or {}
    run = snapshot.get('run_options') or {}
    storage = snapshot.get('storage') or {}
    result = {}
    for key in sorted(FIELDS):
        native = NATIVE_POLICY_NAMES.get(key, key)
        if key == 'env_options':
            exists = False
            original = None
            scope = 'test_environment'
        elif key in POLICY_FIELDS:
            exists = bool(policy.get('available')) and native in props
            original = props.get(native)
            scope = 'test' if policy is row and row else 'global'
        elif key == 'run_mode':
            mode = snapshot.get('run_mode')
            exists = bool(mode.get('available')) if isinstance(mode, dict) else mode is not None
            original = mode.get('value') if isinstance(mode, dict) else mode
            scope = 'session'
        elif key in RUN_FIELDS:
            exists = bool(run.get('available')) and key in run
            original = run.get(key)
            scope = 'run_options'
        else:
            item = storage.get(key) or {}
            exists = bool(item.get('available')) if isinstance(item, dict) else False
            original = item.get('value') if isinstance(item, dict) else None
            scope = 'storage'
        result[key] = {'source': scope if exists else 'unknown', 'available': exists,
                       'original': original, 'override': overrides.get(key),
                       'overridden': key in overrides,
                       'effective': overrides[key] if key in overrides else original,
                       'readback': None, 'verified': False}
    return result


def clone_setup_without_history(source_dir, target_dir):
    """Copy a saved setup before first open and remove only history entries.

    Reject symlinks and non-XML databases instead of following paths or guessing.
    The caller must choose a new owned destination.
    """
    source_dir = os.path.realpath(source_dir)
    target_dir = os.path.abspath(target_dir)
    if not os.path.isdir(source_dir) or os.path.lexists(target_dir):
        raise ValueError('Source must exist and private destination must not exist')
    if os.path.commonpath([source_dir, os.path.realpath(target_dir)]) == source_dir:
        raise ValueError('Private setup cannot be inside the source setup')
    for root, dirs, files in os.walk(source_dir, followlinks=False):
        if root == source_dir:
            dirs[:] = [name for name in dirs if name not in ('results', 'test_states')]
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                raise ValueError('Source setup contains a symlink: ' + name)
    for name in ('maestro.sdb', 'active.state', 'master.tag'):
        if not os.path.isfile(os.path.join(source_dir, name)):
            raise ValueError('Source setup is missing ' + name)
    def checksum(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    original = {name: checksum(os.path.join(source_dir, name)) for name in ('maestro.sdb', 'active.state')}
    try:
        def ignore(directory, names):
            ignored = set(shutil.ignore_patterns('*.cdslck', '*.lck', '*~')(directory, names))
            if directory == source_dir:
                ignored.update(set(names) & {'results', 'test_states'})
            return ignored
        shutil.copytree(source_dir, target_dir, ignore=ignore)
        for name in original:
            if checksum(os.path.join(source_dir, name)) != original[name] or checksum(os.path.join(target_dir, name)) != original[name]:
                raise ValueError('Source Maestro changed during private copy: ' + name)
        count = 0
        for name in ('maestro.sdb', 'active.state'):
            path = os.path.join(target_dir, name)
            tree = ET.parse(path)
            changed = False
            for parent in tree.iter():
                for child in list(parent):
                    if str(child.tag).rsplit('}', 1)[-1].lower() == 'historyentry':
                        parent.remove(child)
                        count += 1
                        changed = True
            if changed:
                tree.write(path, encoding='utf-8', xml_declaration=True)
        return count
    except BaseException:
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir)
        raise


def project_tool_setup(source_dir, generated_dir, target_dir, source_test, run_mode=None):
    """Project a generated tool test into an owned copy of the saved setup.

    The source copy is never opened before its history entries are removed.
    Only current test/corner/variable definitions are replaced. Native reopen
    and readback remain mandatory before the setup can be used.
    """
    count = clone_setup_without_history(source_dir, target_dir)
    try:
        source_db_path = os.path.join(target_dir, 'maestro.sdb')
        db = ET.parse(source_db_path)
        fresh = ET.parse(os.path.join(generated_dir, 'maestro.sdb'))
        active = db.getroot().find('active')
        generated_active = fresh.getroot().find('active')
        if active is None or generated_active is None:
            raise ValueError('Unsupported Maestro active setup structure')
        if run_mode is not None:
            modes = active.findall('currentmode')
            if len(modes) != 1 or not isinstance(run_mode, str) or not run_mode:
                raise ValueError('Unsupported private Maestro run mode')
            modes[0].text = run_mode
        for tag in ('tests', 'corners', 'vars'):
            replacements = generated_active.findall(tag)
            if len(replacements) > 1 or (tag in ('tests', 'corners') and not replacements):
                raise ValueError('Unsupported generated Maestro ' + tag)
            original = active.findall(tag)
            index = list(active).index(original[0]) if original else len(active)
            for child in original:
                active.remove(child)
            for child in replacements:
                active.insert(index, copy.deepcopy(child))
                index += 1
        db.write(source_db_path, encoding='utf-8', xml_declaration=True)
        # Keep simulator and environment options from the selected test; the
        # fixture's own analyses, model, variables and outputs remain authoritative.
        state_path = os.path.join(target_dir, 'active.state')
        state = ET.parse(state_path)
        generated_state = ET.parse(os.path.join(generated_dir, 'active.state'))
        source_rows = [x for x in state.getroot().findall('Test') if x.get('Name') == source_test]
        new_rows = generated_state.getroot().findall('Test')
        if len(source_rows) != 1 or len(new_rows) != 1:
            raise ValueError('Source or generated Maestro test identity is ambiguous')
        source_test_node = copy.deepcopy(source_rows[0])
        tool_test = new_rows[0]
        source_test_node.set('Name', tool_test.get('Name'))
        # Replace the fixture-owned data only. Retain source settings whose
        # semantics are unrelated to the measurement fixture.
        owned = frozenset(('adeInfo', 'rfstim', 'spList', 'analyses', 'nil',
                           'outputs', 'paramSetup', 'modelSetup', 'variables', 'graphicalStimuli'))
        for component in list(source_test_node):
            if component.tag == 'component' and component.get('Name') in owned:
                source_test_node.remove(component)
        for component in tool_test:
            if component.tag == 'component' and component.get('Name') in owned:
                source_test_node.append(copy.deepcopy(component))
        root = generated_state.getroot()
        index = list(root).index(tool_test)
        root.remove(tool_test)
        root.insert(index, source_test_node)
        generated_state.write(state_path, encoding='utf-8', xml_declaration=True)
        return count
    except BaseException:
        shutil.rmtree(target_dir)
        raise

PUBLIC_POLICY_FIELDS = frozenset('blockemail configuretimeout configuredtimeout distributionmethod lingertimeout interfacename jobhostname jobqueue joboutfilename joberrfilename jobjobname loginshell jobresourcerequirements jobprojectname jobsubmitcommand jobusergroup maxjobs parallelnumprocs olvjobresourcerequirements olvjobprojectname olvjobusergroup olvpriority olvJobGroup preemptivestart reconfigureimmediately runtimeout sgehardresource sgesoftresource sgepriority sgeparallelenv showerrorwhenretrying showoutputlogerror startmaxjobsimmed starttimeout usesameprocess'.split())


def _skill(value):
    if value is None or value is False:
        return 'nil'
    if value is True:
        return 't'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple)):
        return 'list(' + ' '.join(_skill(x) for x in value) + ')'
    raise ValueError('Unsupported native setting type')


def runtime_apply_skill(runtime, target_test, report_path, policy_suffix):
    """Apply captured scalar native policies in isolated HOME and require readback."""
    snapshot = runtime.get('snapshot') or {}
    overrides = normalize_overrides(runtime.get('overrides'))
    source_test = selected_test(runtime)
    global_row = snapshot.get('global') or {}
    test_row = next((x for x in snapshot.get('tests', []) if x.get('name') == source_test), {})
    lines = []
    policy_overrides = {NATIVE_POLICY_NAMES.get(k, k): str(v) for k, v in overrides.items() if k in POLICY_FIELDS}
    for scope, row, target in (('global', global_row, None), ('test', test_row, target_test)):
        if not row.get('available'):
            if policy_overrides:
                raise ValueError('Cannot apply job overrides: source ' + scope + ' policy unavailable')
            continue
        if row.get('opaque'):
            raise ValueError('Cannot reproduce opaque ' + scope + ' policy fields: ' + ', '.join(sorted(row['opaque'])))
        props = dict(row.get('properties') or {})
        props.pop('name', None)
        props.update(policy_overrides)
        if any(not isinstance(key, str) or not key.replace('_', '').isalnum() or key[0].isdigit() for key in props):
            raise ValueError('Invalid native policy property')
        name = 'CAL_' + policy_suffix + '_' + scope
        args = ' '.join('?' + key + ' ' + _skill(value) for key, value in sorted(props.items()) if key in PUBLIC_POLICY_FIELDS)
        testarg = ' ?tests list(' + _skill(target) + ')' if target else ''
        lines.append('unless(maeSetJobPolicy(%s %s%s ?session s) error("Cannot apply private %s job policy"))' % (_skill(name), args, testarg, scope))
        for key, value in sorted(props.items()):
            if key not in PUBLIC_POLICY_FIELDS:
                lines.append('unless(axlSetJobPolicyProperty(%s %s %s) error("Cannot preserve job property %s"))' % (_skill(name), _skill(key), _skill(value), key))
        getarg = ' ?testName ' + _skill(target) if target else ''
        lines.append('calPolicy=maeGetJobPolicy(?session s%s)' % getarg)
        lines.append('unless(calPolicy error("Missing %s job policy readback"))' % scope)
        for key, value in sorted(props.items()):
            lines.append('unless(equal(get(calPolicy \'%s) %s) error("%s job property readback mismatch: %s"))' % (key, _skill(value), scope, key))
    options = snapshot.get('run_options') or {}
    runvalues = {k: options[k] for k in ('RunIn', 'DivideJobs') if options.get('available') and k in options and options[k] is not None}
    for k in ('RunIn', 'DivideJobs'):
        if k in overrides:
            runvalues[k] = overrides[k]
    limits = overrides.get('JobLimit', options.get('JobLimit'))
    if isinstance(limits, dict):
        limits = [[target_test if key == source_test else key, value] for key, value in limits.items()]
    elif isinstance(limits, list):
        limits = [[target_test if row[0] == source_test else row[0], row[1]] for row in limits]
    if limits:
        runvalues['JobLimit'] = limits
    if runvalues:
        def optval(k, v):
            return 'stringToSymbol(%s)' % _skill(v) if k in ('RunIn', 'DivideJobs') and isinstance(v, str) else _skill(v)
        args = ' '.join('?' + k + ' ' + optval(k, v) for k, v in sorted(runvalues.items()))
        lines.append('calSetup=axlGetActiveSetup(axlGetMainSetupDB(s))')
        lines.append('unless(axlSetRunDistributeOptions(calSetup %s) error("Cannot apply private Run Options"))' % args)
        lines.append('calRun=axlGetRunDistributeOptions(calSetup)')
        for key, value in sorted(runvalues.items()):
            lines.append('unless(equal(get(calRun \'%s) %s) error("Run Options readback mismatch: %s"))' % (key, optval(key,value), key))
    mode = snapshot.get('run_mode') or {}
    expected_mode = overrides.get('run_mode', mode.get('value') if isinstance(mode, dict) and mode.get('available') else None)
    if isinstance(expected_mode, str):
        lines.append('unless(equal(maeGetCurrentRunMode(?session s) %s) error("Run mode readback mismatch"))' % _skill(expected_mode))
    env = overrides.get('env_options') or {}
    if env:
        pairs = 'list(' + ' '.join('list(%s %s)' % (_skill(k), _skill(v)) for k,v in sorted(env.items())) + ')'
        lines.append('unless(maeSetEnvOption(%s ?options %s ?session s) error("Cannot set test environment"))' % (_skill(target_test), pairs))
        for k, v in sorted(env.items()):
            lines.append('unless(equal(maeGetEnvOption(%s ?option %s ?session s) %s) error("Environment option readback mismatch"))' % (_skill(target_test), _skill(k), _skill(v)))
    lines.append('unless(maeSetEnvOption(%s ?options list(list("controlMode" "batch")) ?session s) error("Cannot set batch control"))' % _skill(target_test))
    lines.append('unless(equal(maeGetEnvOption(%s ?option "controlMode" ?session s) "batch") error("Batch control readback mismatch"))' % _skill(target_test))
    lines.append('o=outfile(%s)' % _skill(report_path))
    lines.append('fprintf(o "global %%L\\ntest %%L\\nmode %%L\\nrun_options %%L\\n" maeGetJobPolicy(?session s) maeGetJobPolicy(?session s ?testName %s) maeGetCurrentRunMode(?session s) axlGetRunDistributeOptions(axlGetActiveSetup(axlGetMainSetupDB(s))))' % _skill(target_test))
    lines.append('calRun=axlGetRunDistributeOptions(axlGetActiveSetup(axlGetMainSetupDB(s)))')
    lines.append('fprintf(o "RunIn %L\\nDivideJobs %L\\nJobLimit %L\\n" calRun->RunIn calRun->DivideJobs calRun->JobLimit)')
    for k in sorted(env):
        lines.append('fprintf(o "env %s %%L\\n" maeGetEnvOption(%s ?option %s ?session s))' % (k, _skill(target_test), _skill(k)))
    for key, (group, name, kind) in sorted(STORAGE_NATIVE.items()):
        lines.append('fprintf(o "storage %s %%L\\n" envGetVal(%s %s))' % (key, _skill(group), _skill(name)))
    lines.append('close(o)')
    return '\n'.join(lines) + '\n'

STORAGE_NATIVE = {'project_dir': ('asimenv.startup', 'projectDir', 'string'),
    'save_res_dir': ('adexl.results', 'saveResDir', 'string'),
    'save_dir': ('adexl.results', 'saveDir', 'string'),
    'use_local_psf_dir': ('adexl.results', 'useLocalPsfDir', 'boolean'),
    'local_psf_dir': ('adexl.results', 'saveLocalPsfDir', 'string'),
    'use_local_rdb_dir': ('adexl.results', 'useLocalRdbDir', 'boolean'),
    'local_rdb_dir': ('adexl.results', 'saveLocalRdbDir', 'string'),
    'fallback': ('adexl.results', 'fallbackToDirWhenSimResultsUnspecified', 'cyclic')}


def storage_apply_skill(runtime):
    values = effective_runtime(runtime)
    lines = []
    source = runtime.get('source') or {}
    for key, (group, name, kind) in STORAGE_NATIVE.items():
        row = values[key]
        if not row['available'] and not row['overridden']:
            continue
        value = row['effective']
        if kind in ('string', 'cyclic'):
            if not isinstance(value, str):
                continue
            if kind == 'string' and value.startswith('~/'):
                storage = (runtime.get('snapshot') or {}).get('storage') or {}
                original_home = (storage.get('source_home') or {}).get('value')
                if not original_home: raise ValueError('Source HOME is required for a relative home path')
                value = os.path.join(original_home, value[2:])
            elif kind == 'string' and value and not os.path.isabs(value) and not value.startswith('$'):
                captured_storage = (runtime.get('snapshot') or {}).get('storage') or {}
                workdir = (captured_storage.get('source_work_dir') or {}).get('value') or source.get('workspace') or os.getcwd()
                value = os.path.abspath(os.path.join(workdir, value))
        else:
            if value not in (True, False, None):
                raise ValueError('Unsupported storage boolean: ' + key)
        lines.append('envSetVal(%s %s \'%s %s)' % (_skill(group), _skill(name), kind, _skill(value)))
        lines.append('unless(equal(envGetVal(%s %s) %s) error("Storage readback mismatch: %s"))' % (_skill(group), _skill(name), _skill(value), key))
    return '\n'.join(lines) + '\n'


def runtime_preopen_skill(runtime, home, suffix):
    snapshot = runtime.get('snapshot') or {}
    test = selected_test(runtime)
    overrides = normalize_overrides(runtime.get('overrides'))
    add = {NATIVE_POLICY_NAMES.get(k, k): str(v) for k,v in overrides.items() if k in POLICY_FIELDS}
    rows = [('global', snapshot.get('global') or {}),
            ('test', next((x for x in snapshot.get('tests', []) if x.get('name') == test), {}))]
    result = []
    for scope, row in rows:
        if not row.get('available'):
            continue
        if row.get('opaque'):
            raise ValueError('Cannot reproduce opaque policy fields before setup open')
        props = dict(row.get('properties') or {})
        source_name = props.pop('name', None)
        props.update(add)
        for name in [source_name, 'CAL_' + suffix + '_' + scope]:
            if not isinstance(name, str) or not name:
                continue
            if len(name) > 255 or name in ('.', '..') or '/' in name or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise ValueError('Unsafe native policy name')
            fields = dict(props, name=name)
            if any(not isinstance(k, str) or not k.replace('_','').isalnum() or k[0].isdigit() for k in fields):
                raise ValueError('Invalid policy key')
            plist = 'list(nil ' + ' '.join("'%s %s" % (k, _skill(v)) for k,v in sorted(fields.items())) + ')'
            result.append('unless(axlAddJobPolicy(%s %s %s) error("Cannot create private policy alias"))' % (_skill(name), _skill(os.path.join(home, '.cadence')), plist))
    return '\n'.join(result) + '\n'


def simulation_monitor_root(runtime, manifest):
    """Return only the exact private LCV subtree when its simulation root is known."""
    values = effective_runtime(runtime)
    local = values['use_local_psf_dir']
    if local['effective'] is True:
        return None
    result = values['save_res_dir']
    project = values['project_dir']
    if result['effective']:
        root = result['effective']
    elif (result['available'] or result['overridden']) and (project['available'] or project['overridden']):
        fallback = values['fallback']['effective']
        database = values['save_dir']['effective']
        if fallback == 'RdbDir' and database:
            root = database
        elif fallback and fallback != 'RdbDir':
            return None
        else:
            root = project['effective']
    elif not result['available'] and not project['available'] and not result['overridden'] and not project['overridden']:
        root = os.path.join(manifest['folder'], 'simulation')
    else:
        return None
    if not isinstance(root, str) or not root or '$' in root:
        return None
    storage = (runtime.get('snapshot') or {}).get('storage') or {}
    if root.startswith('~/'):
        home = (storage.get('source_home') or {}).get('value')
        if not home: return None
        root = os.path.join(home, root[2:])
    elif not os.path.isabs(root):
        workdir = (storage.get('source_work_dir') or {}).get('value') or (runtime.get('source') or {}).get('workspace')
        if not workdir: return None
        root = os.path.join(workdir, root)
    return os.path.join(os.path.abspath(root), manifest['library'], manifest['cell'], 'maestro', 'results', 'maestro')
