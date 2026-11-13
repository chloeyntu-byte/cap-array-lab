"""Native AC terminal-power accounting, with explicit completeness checks.

Voltage phasors are referenced to the simulation ground; current phasors are
positive INTO each module terminal. Include *all* electrical terminals,
including supplies, and group mutually coupled elements when individual net
power includes coupling transfer. Phasors must use the same AC excitation and
frequency. This module does not infer terminal currents from C/Q or SP data.
"""
import math


def _complex(value):
    z = complex(value)
    if not math.isfinite(z.real) or not math.isfinite(z.imag):
        raise ValueError('Terminal voltage and current must be finite.')
    return z


def terminal_power(terminals):
    """Average watts for peak AC phasors: 1/2 Re(sum(V * conj(I)))."""
    if not terminals:
        raise ValueError('All module terminal phasors are required.')
    return 0.5 * sum((_complex(t['voltage']) * _complex(t['current']).conjugate()).real
                     for t in terminals)


def loss_percentages(modules, input_power_W, complete=False, rel_tolerance=1e-5):
    """Account measured module powers against input power, or withhold shares.

    `modules` maps unique module/group names to complete terminal lists.
    `complete=True` asserts all dissipative paths and all module terminals are
    included. Active power generation and net transfer from coupled elements are
    not passive dissipative losses; negative module powers invalidate shares.
    """
    result = {'available': False, 'modules': {}, 'reason': ''}
    if not complete or not modules:
        result['reason'] = 'Complete native terminal-current data is unavailable.'
        return result
    power = float(input_power_W)
    if not math.isfinite(power) or power <= 0:
        result['reason'] = 'Input power is not positive and finite.'
        return result
    if not math.isfinite(rel_tolerance) or rel_tolerance < 0:
        raise ValueError('Invalid power balance tolerance.')
    powers = {name: terminal_power(terminals) for name, terminals in modules.items()}
    residual = power - sum(powers.values())
    result.update(input_power_W=power, residual_W=residual,
                  module_power_W=powers)
    if abs(residual) > power * rel_tolerance:
        result['reason'] = 'Terminal power does not balance input power.'
        return result
    if any(p < -power * rel_tolerance for p in powers.values()):
        result['reason'] = 'A module supplies power or transfers power through coupling; group it before assigning loss.'
        return result
    result.update(available=True, modules={name: 100.0 * max(0.0, p) / power for name, p in powers.items()},
                  reason='')
    return result


def _sq(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def fixture_plan(project, cfg):
    """Plan private per-bit series probes; never modify the source schematic.

    Call `pin_label(plan, instance_id, master_pin_name)` instead of original
    label while placing project instances. Append `fixture_skill(plan)` after
    all modules have been placed. Each probe PLUS is the original net and its
    MINUS is the isolated module pin. Register drivers are not measured as DUT
    modules. Empty-pin coupling primitives are retained in the group metadata.
    """
    from .model import expand_pin
    probes, labels = [], {}
    for inst in project['instances']:
        if not inst.get('enabled', True):
            continue
        for pin in inst['symbol']['pins']:
            isolated = []
            for bit in expand_pin(pin['name']):
                index = len(probes)
                name = 'CAL_LS{}'.format(index)
                node = 'CAL_LN{}'.format(index)
                probes.append(dict(instance=inst['id'], pin=bit, name=name,
                                   node=node, net=cfg['nets'][inst['id'] + '.' + bit]))
                isolated.append(node)
            labels.setdefault(inst['id'], {})[pin['name']] = ','.join(isolated)
    return {'probes': probes, 'labels': labels,
            'instances': [inst for inst in project['instances'] if inst.get('enabled', True)],
            'port_fixture': 'ground_50ohm_each' if cfg.get('decomposition') else 'differential_50ohm'}


def pin_label(plan, instance_id, pin_name):
    return plan['labels'][instance_id][pin_name]


def fixture_skill(plan):
    """Place 0 V sense sources and a DC-zero 1 A floating AC source.

    Current source MINUS is connected to P, PLUS to N, so 1 A is injected
    into P and extracted from N. The SP analysis ignores its AC stimulus.
    """
    body = []
    for index, p in enumerate(plan['probes']):
        body.append('calSource(cv {} list({} {}) {} {} "0" "0" "0")\n'.format(
            _sq(p['name']), 20 + (index % 8)*2, 5 + (index // 8)*2,
            _sq(p['net']), _sq(p['node'])))
    body.append('''m=dbOpenCellViewByType("analogLib" "isource" "symbol" "" "r")
unless(m error("Missing analogLib isource"))
i=dbCreateInst(cv m "CAL_LOSS_I" list(20 2) "R0")
dbReplaceProp(i "srcType" "string" "dc")
dbReplaceProp(i "idc" "string" "0")
dbReplaceProp(i "smallSig" "boolean" t)
dbReplaceProp(i "acm" "string" "1-CAL_LOSS_MODE")
dbReplaceProp(i "acp" "string" "0")
calStub(cv i "PLUS" "CAL_N") calStub(cv i "MINUS" "CAL_P")
i=dbCreateInst(cv m "CAL_LOSS_CM" list(22 2) "R0")
dbReplaceProp(i "srcType" "string" "dc")
dbReplaceProp(i "idc" "string" "0")
dbReplaceProp(i "smallSig" "boolean" t)
dbReplaceProp(i "acm" "string" "CAL_LOSS_MODE")
dbReplaceProp(i "acp" "string" "0")
calStub(cv i "PLUS" "CAL_VSS") calStub(cv i "MINUS" "CAL_P")
''')
    return ''.join(body)


def setup_skill(plan, test_name, frequencies):
    """Enable simultaneous native AC and save probe currents in the SP test."""
    values = 'list(' + ' '.join(_sq('{:.17g}'.format(f)) for f in frequencies) + ')'
    body = ['unless(maeSetAnalysis({} "ac" ?options list(list("sweep" "Frequency") list("useDiscrete" t) list("values" {})) ?session s) error("Cannot set native loss AC analysis"))\n'.format(_sq(test_name), values)]
    for index, p in enumerate(plan['probes']):
        expression = 'i("/{}/PLUS" ?result "ac")'.format(p['name'])
        body.append('unless(maeAddOutput({} {} ?expr {} ?plot nil ?save t ?session s) error("Cannot save loss probe current"))\n'.format(_sq('CAL_LOSS_SAVE_{}'.format(index)), _sq(test_name), _sq(expression)))
    return ''.join(body)


def reader_skill(plan):
    """Emit LOSS TSV records in the current done test's openResults context.

    Each waveform is checked against the actual input-node frequency grid.
    The caller associates the records with the immediately preceding TEST row.
    """
    body = ['''  calLP=getData("CAL_P" ?result "ac") calLN=getData("CAL_N" ?result "ac")
  unless(calLP && calLN error("Missing native loss excitation voltages"))
  calLX=drGetWaveformXVec(calLP) calLNX=drGetWaveformXVec(calLN) calLPV=drGetWaveformYVec(calLP) calLNV=drGetWaveformYVec(calLN)
  unless(drVectorLength(calLX)==drVectorLength(calLNV) error("Loss voltage grid mismatch"))
  for(calLI 0 drVectorLength(calLX)-1
    unless(drGetElem(calLX calLI)==drGetElem(calLNX calLI) error("Loss input frequencies differ"))
    calLA=drGetElem(calLPV calLI) calLB=drGetElem(calLNV calLI)
    fprintf(o "LOSS_INPUT\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\n" drGetElem(calLX calLI) real(calLA) imag(calLA) real(calLB) imag(calLB)))
''']
    for index, p in enumerate(plan['probes']):
        body.append('''  calLV=getData({node} ?result "ac") calLC=getData({current} ?result "ac")
  unless(calLV && calLC error("Missing native loss terminal waveform"))
  calLXV=drGetWaveformXVec(calLV) calLXI=drGetWaveformXVec(calLC)
  calLVV=drGetWaveformYVec(calLV) calLCV=drGetWaveformYVec(calLC)
  unless(drVectorLength(calLX)==drVectorLength(calLVV) && drVectorLength(calLX)==drVectorLength(calLCV) error("Loss terminal grid mismatch"))
  for(calLI 0 drVectorLength(calLX)-1
    unless(drGetElem(calLX calLI)==drGetElem(calLXV calLI) && drGetElem(calLX calLI)==drGetElem(calLXI calLI) error("Loss sample frequencies differ"))
    calLA=drGetElem(calLVV calLI) calLB=drGetElem(calLCV calLI)
    fprintf(o "LOSS_PIN\\t%d\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\n" {index} drGetElem(calLX calLI) real(calLA) imag(calLA) real(calLB) imag(calLB)))
'''.format(node=_sq(p['node']), current=_sq(p['name'] + ':p'), index=index))
    return ''.join(body)


def parse_records(lines, plan):
    """Parse one completed test's LOSS records into JSON-safe sample dictionaries."""
    samples = {}
    expected = len(plan['probes'])
    for line in lines:
        fields = line.rstrip('\n').split('\t')
        if fields[0] == 'LOSS_INPUT':
            if len(fields) != 6:
                raise ValueError('Malformed loss input record.')
            f, pr, pi, nr, ni = map(float, fields[1:])
            item = samples.setdefault(f, {'frequency_Hz': f, 'pins': {}})
            if 'input' in item:
                raise ValueError('Duplicate loss input sample.')
            item['input'] = [pr, pi, nr, ni]
        elif fields[0] == 'LOSS_PIN':
            if len(fields) != 7:
                raise ValueError('Malformed loss pin record.')
            idx = int(fields[1])
            if idx < 0 or idx >= expected:
                raise ValueError('Unknown native loss probe.')
            f, vr, vi, ir, ii = map(float, fields[2:])
            item = samples.setdefault(f, {'frequency_Hz': f, 'pins': {}})
            if str(idx) in item['pins']:
                raise ValueError('Duplicate loss probe sample.')
            item['pins'][str(idx)] = [vr, vi, ir, ii]
    rows = [samples[f] for f in sorted(samples)]
    if not rows or any('input' not in row or len(row['pins']) != expected for row in rows):
        raise ValueError('Incomplete native loss waveforms.')
    if any(not math.isfinite(v) for row in rows for v in
           ([row['frequency_Hz']] + row['input'] + [n for p in row['pins'].values() for n in p])):
        raise ValueError('Non-finite native loss waveform.')
    return rows


def combine_records(differential, common, plan):
    """Pair independently simulated excitation waveforms by exact frequency."""
    if len(differential) != len(common):
        raise ValueError('Loss excitation sweep sizes differ.')
    out = []
    for a, b in zip(differential, common):
        if a['frequency_Hz'] != b['frequency_Hz'] or set(a['pins']) != set(b['pins']):
            raise ValueError('Loss excitation identities or frequency grids differ.')
        item = dict(a)
        item['cm_input'] = b['input']
        item['cm_pins'] = b['pins']
        out.append(item)
    return out


def reconstruct_sample(sample, plan, f=None):
    """Normalize one native sample to a floating 1 A peak DUT drive.

    Returns the P/N voltages, normalized module pin phasors and fixture/source
    powers, or an unavailable result. Shared by loss accounting and tank scan.
    """
    f = sample['frequency_Hz'] if f is None else f
    vp = complex(*sample['input'][:2])
    vn = complex(*sample['input'][2:])
    fixture = plan.get('port_fixture', 'none')
    if fixture == 'ground_50ohm_each':
        if 'cm_input' not in sample or 'cm_pins' not in sample:
            return {'available': False, 'reason': 'Missing independent common-mode native AC excitation.',
                    'modules': {}, 'frequency_Hz': f, 'estimated': False}
        cp = complex(*sample['cm_input'][:2])
        cn = complex(*sample['cm_input'][2:])
        # Difference and P-to-ground source magnitudes are both 1 A peak.
        # KCL gives DUT total current after removing two measured 50-ohm loads.
        id_sum = -(vp + vn) / 50.0
        ic_sum = 1 - (cp + cn) / 50.0
        common_scale = max(1.0, abs(cp/50.0), abs(cn/50.0))
        diff_scale = max(1.0, abs(vp/50.0), abs(vn/50.0))
        if abs(ic_sum) <= 1e-12 * common_scale:
            if abs(id_sum) > 1e-12 * diff_scale:
                return {'available': False, 'reason': 'Floating common mode is singular or ill-conditioned.',
                        'modules': {}, 'frequency_Hz': f, 'estimated': False}
            coefficient = 0j  # Free common-mode gauge: differential drive already satisfies KCL.
        else:
            coefficient = -id_sum / ic_sum
        vp += coefficient * cp
        vn += coefficient * cn
        dut_current = 1 + coefficient - vp / 50.0
        if abs(dut_current) < 1e-12:
            return {'available': False, 'reason': 'Floating differential current is unresolved.',
                    'modules': {}, 'frequency_Hz': f, 'estimated': False}
        # A consistent 1 A DUT current normalization makes endpoint powers
        # comparable when the 50-ohm fixture draws frequency-dependent current.
        vp /= dut_current
        vn /= dut_current
        pins = {}
        for key, value in sample['pins'].items():
            other = sample['cm_pins'][key]
            voltage = (complex(*value[:2]) + coefficient * complex(*other[:2])) / dut_current
            current = (complex(*value[2:]) + coefficient * complex(*other[2:])) / dut_current
            pins[key] = [voltage.real, voltage.imag, current.real, current.imag]
        sample = dict(sample, pins=pins)
        source_power = 0.5 * (vp * ((1+coefficient)/dut_current).conjugate() - vn * (1/dut_current).conjugate()).real
        fixture_power = 0.5 * (abs(vp)**2 + abs(vn)**2) / 50.0
        condition = 'floating P/N reconstructed from two native AC excitations'
    elif fixture == 'differential_50ohm':
        dut_current = 1 - (vp-vn)/50.0
        if abs(dut_current) < 1e-12:
            return {'available': False, 'reason': 'Floating differential current is unresolved.',
                    'modules': {}, 'frequency_Hz': f, 'estimated': False}
        vp /= dut_current
        vn /= dut_current
        pins = {}
        for key, value in sample['pins'].items():
            voltage = complex(*value[:2]) / dut_current
            current = complex(*value[2:]) / dut_current
            pins[key] = [voltage.real, voltage.imag, current.real, current.imag]
        sample = dict(sample, pins=pins)
        source_power = 0.5 * ((vp-vn) * (1/dut_current).conjugate()).real
        fixture_power = 0.5 * abs(vp-vn)**2 / 50.0
        condition = 'floating P/N AC; differential 50-ohm fixture loss excluded'
    elif fixture == 'none':
        source_power = 0.5 * (vp-vn).real
        fixture_power = 0.0
        condition = 'floating P/N AC'
    else:
        raise ValueError('Unknown port fixture.')
    power = 0.5 * (vp-vn).real  # DUT differential current is normalized to 1 A peak.
    balance_error = source_power-fixture_power-power
    if abs(balance_error) > 1e-10 * max(abs(source_power), abs(fixture_power), abs(power), 1e-300):
        return {'available': False, 'reason': 'Native source and fixture power are inconsistent.',
                'modules': {}, 'frequency_Hz': f, 'estimated': False}
    return {'available': True, 'vp': vp, 'vn': vn, 'sample': sample, 'power': power,
            'source_power': source_power, 'fixture_power': fixture_power, 'condition': condition}


def evaluate_sample(rows, plan, frequency, groups=None, overrides=None):
    """Calculate native sample powers or interpolate independently balanced powers.

    groups maps each coupled instance ID to a common group label. A loss-power
    override may replace a measured net terminal power for a verified device
    model (e.g. series-R inductor); use paired overrides for every member of a
    coupled block, and always check total power balance.
    """
    import bisect
    f = float(frequency)
    if not math.isfinite(f) or f <= 0 or not rows:
        raise ValueError('A positive frequency and native loss samples are required.')
    xs = [row['frequency_Hz'] for row in rows]
    if any(b <= a for a, b in zip(xs, xs[1:])):
        raise ValueError('Loss samples must have unique sorted frequencies.')
    i = bisect.bisect_left(xs, f)
    if i == len(xs) or (i == 0 and f < xs[0]):
        raise ValueError('Frequency is outside the native loss sweep.')
    if xs[i] == f:
        sample = rows[i]
    else:
        # Each endpoint was independently solved and balanced by Spectre.
        # Linear interpolation of those *powers* preserves that balance;
        # interpolating V and I separately does not solve the intermediate
        # circuit and generally violates energy conservation.
        left = evaluate_sample(rows, plan, xs[i-1], groups, overrides)
        right = evaluate_sample(rows, plan, xs[i], groups, overrides)
        if not left['available'] or not right['available']:
            return {'available': False, 'reason': 'Native bracket loss unavailable: ' +
                    (left.get('reason') or right.get('reason') or ''),
                    'modules': {}, 'frequency_Hz': f, 'estimated': True}
        if set(left['module_power_W']) != set(right['module_power_W']):
            raise ValueError('Loss module identities differ between samples.')
        t = (f-xs[i-1])/(xs[i]-xs[i-1])
        power = left['input_power_W'] + t*(right['input_power_W']-left['input_power_W'])
        powers = {name: left['module_power_W'][name] + t*(right['module_power_W'][name]-left['module_power_W'][name])
                  for name in left['module_power_W']}
        return {'available': True, 'reason': '', 'frequency_Hz': f,
                'input_power_W': power, 'module_power_W': powers,
                'modules': {name: 100*max(0.0, value)/power for name, value in powers.items()},
                'residual_W': power-sum(powers.values()), 'estimated': True,
                'bracket_Hz': [xs[i-1], xs[i]],
                'method': 'linear interpolation of native endpoint powers',
                'source_power_W': left.get('source_power_W', power) + t*(right.get('source_power_W', power)-left.get('source_power_W', power)),
                'fixture_power_W': left.get('fixture_power_W', 0) + t*(right.get('fixture_power_W', 0)-left.get('fixture_power_W', 0)),
                'excitation_condition': left.get('excitation_condition', ''), 'modeled_winding_losses': left.get('modeled_winding_losses', []), 'coupled_groups': left.get('coupled_groups', [])}
    rec = reconstruct_sample(sample, plan, f)
    if not rec['available']:
        return rec
    vp, vn, sample, power = rec['vp'], rec['vn'], rec['sample'], rec['power']
    source_power, fixture_power, condition = rec['source_power'], rec['fixture_power'], rec['condition']
    groups = dict(groups or {})
    instances = {node['id']: node for node in plan.get('instances', [])}
    if instances and not isinstance(next(iter(instances.values())), dict):
        raise ValueError('Missing native device metadata.')
    # Build connected components of mutual coupling. If every winding is the
    # verified mode-1 indq model, calculate each winding's resistor heat from
    # its measured actual current. Otherwise keep the entire block together.
    parent = {}
    def root(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = root(parent[x])
        return parent[x]
    mutuals = []
    for node in instances.values():
        if node.get('kind') == 'passive' and node.get('cell') == 'mind':
            a, b = node['props']['ind1'], node['props']['ind2']
            parent[root(a)] = root(b)
            mutuals.append(node['id'])
    components = {}
    for name in parent:
        components.setdefault(root(name), []).append(name)
    resolved = set()
    coupled_groups = []
    for names in components.values():
        supported = all(name in instances and instances[name].get('cell') == 'indq'
                        and str(instances[name]['props'].get('mode')) == '1' for name in names)
        if supported:
            resolved.update(names)
        else:
            group = 'Coupled: ' + ', '.join(sorted(names))
            coupled_groups.append({'name': group, 'members': sorted(names)})
            for name in names:
                groups[name] = group
    terminals = {}
    winding_currents = {}
    for index, probe in enumerate(plan['probes']):
        p = sample['pins'][str(index)]
        name = probe['instance']
        terminals.setdefault(groups.get(name, name), []).append(
            {'voltage': complex(p[0], p[1]), 'current': complex(p[2], p[3])})
        if name in resolved and probe['pin'] == 'PLUS':
            winding_currents[name] = complex(p[2], p[3])
    from .model import number
    for name in resolved:
        if name not in winding_currents:
            raise ValueError('Missing measured coupled-inductor PLUS current.')
        props = instances[name]['props']
        resistance = 2 * math.pi * number(props['fq']) * number(props['l']) / number(props['q'])
        heat = 0.5 * resistance * abs(winding_currents[name])**2
        terminals[name] = [{'voltage': complex(2 * heat), 'current': 1+0j}]
    for name in mutuals:
        # Native ideal mutual coupling transfers energy without dissipating it.
        terminals[name] = [{'voltage': 0j, 'current': 0j}]
    if overrides:
        result = {'available': False, 'reason': 'External loss overrides are not supported.', 'modules': {}}
    else:
        result = loss_percentages(terminals, power, complete=True)
    result.update(source_power_W=source_power, fixture_power_W=fixture_power, excitation_condition=condition)
    result['modeled_winding_losses'] = sorted(resolved)
    result['coupled_groups'] = coupled_groups
    result['frequency_Hz'] = f
    result['estimated'] = f not in xs
    return result
