"""Track saved changes to an owned private Hierarchy Editor config."""
import hashlib
import re
from pathlib import Path


def _config_path(private):
    if not private.get('cellpath'):
        raise ValueError('Private config location is missing')
    return Path(private['cellpath']) / 'config' / 'expand.cfg'


def config_hash(private):
    return hashlib.sha256(_config_path(private).read_bytes()).hexdigest()


def record_config_baseline(project):
    """Record an initial baseline before an older private config is opened."""
    private = project.get('private') or {}
    if private and not private.get('config_sha256'):
        private['config_sha256'] = config_hash(private)


def capture_saved_config(project):
    """Capture saved private native edits, invalidate results and preserve setup.

    A missing baseline is treated conservatively as an unknown edit. If other
    project changes already require a rebuild, retain that mismatch rather than
    incorrectly declaring the old private schematic current.
    """
    private = project.get('private') or {}
    if not private:
        return False
    path = _config_path(private)
    if not path.is_file():
        return False
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual == private.get('config_sha256'):
        return False
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Saved private config is not UTF-8')
    library, cell = private.get('library'), private.get('cell')
    if not library or not cell:
        raise ValueError('Private config identity is missing')
    # Replace only the generated top identity and its config declaration;
    # source instance/library identities must remain byte-for-byte intact.
    top = library + '.' + cell
    declaration = re.compile(r'(?m)^(\s*config\s+)' + re.escape(cell) + r'(\s*;)')
    if top not in content or not declaration.search(content):
        raise ValueError('Saved private config top identity does not match the owned setup')
    template = content.replace(top, '@CAL_TOP@')
    template = declaration.sub(lambda m: m.group(1) + '@CAL_CELL@' + m.group(2), template, count=1)
    from .backend import digest
    was_current = private.get('digest') == digest(project)
    project['config_template'] = template
    private['config_sha256'] = actual
    for key in ('spectrum', 'spectrum_sha256', 'spectrum_data_sha256', 'result_sha256', 'history'):
        private.pop(key, None)
    if was_current:
        private['digest'] = digest(project)
    return True
