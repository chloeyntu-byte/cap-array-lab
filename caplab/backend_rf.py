"""Private projection of the two RF tests into a saved Maestro setup."""
import copy
from pathlib import Path
import xml.etree.ElementTree as ET


def project_rf_setup(source_dir, generated_dir, target_dir, source_test, run_mode=None):
    from .maestro_runtime import project_tool_setup
    generated = Path(generated_dir); target = Path(target_dir)
    scratch = target.parent / (target.name + '_projection_input')
    scratch.mkdir()
    for name in ('maestro.sdb', 'active.state'):
        (scratch/name).write_bytes((generated/name).read_bytes())
    tree=ET.parse(str(scratch/'active.state')); root=tree.getroot()
    rows={node.get('Name'):node for node in root.findall('Test')}
    if set(rows) != {'CAP_SP','CAP_LOSS_CM'}: raise ValueError('Generated RF test identities are incomplete')
    root.remove(rows['CAP_LOSS_CM'])
    tree.write(str(scratch/'active.state'),encoding='utf-8',xml_declaration=True)
    count=project_tool_setup(source_dir,str(scratch),str(target),source_test,run_mode)
    source_state=ET.parse(str(target/'active.state')); state_root=source_state.getroot()
    projected=[x for x in state_root.findall('Test') if x.get('Name')=='CAP_SP']
    if len(projected)!=1: raise ValueError('Projected RF source test is ambiguous')
    # Preserve every source simulator/environment component and replace only the
    # fixture-owned analyses, outputs, variables and model data for the CM test.
    cm=copy.deepcopy(projected[0]);cm.set('Name','CAP_LOSS_CM')
    generated_state=ET.parse(str(generated/'active.state'))
    generated_cm=[x for x in generated_state.getroot().findall('Test') if x.get('Name')=='CAP_LOSS_CM']
    if len(generated_cm)!=1: raise ValueError('Generated common-mode test is ambiguous')
    owned=frozenset(('adeInfo','rfstim','spList','analyses','nil','outputs','paramSetup','modelSetup','variables','graphicalStimuli'))
    for component in list(cm):
        if component.tag=='component' and component.get('Name') in owned: cm.remove(component)
    for component in generated_cm[0]:
        if component.tag=='component' and component.get('Name') in owned: cm.append(copy.deepcopy(component))
    state_root.append(cm)
    source_state.write(str(target/'active.state'),encoding='utf-8',xml_declaration=True)
    return count
