"""Count every generated arm through the existing controller's actual request body."""
import hashlib,json
from pathlib import Path
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer


def check(manifests:Path,assets:Path):
    from benchmarks.agent_tasks.manifest import load_manifest,read_artifact
    from benchmarks.agent_tasks.coding_controller import Controller,Usage
    tokenizer=Tokenizer.from_file(str(assets/'tokenizer.json'))
    config=json.loads((assets/'tokenizer_config.json').read_bytes())
    env=ImmutableSandboxedEnvironment(trim_blocks=True,lstrip_blocks=True)
    env.filters['tojson']=lambda value,**kw:json.dumps(value,ensure_ascii=False,**kw)
    template=env.from_string(config['chat_template'])
    count=lambda value:len(tokenizer.encode(value,add_special_tokens=False).ids)
    rows=[]
    for path in sorted(manifests.glob('*/manifest.json')):
        manifest,_=load_manifest(path)
        for task in manifest.tasks:
            prompt=read_artifact(path.parent,task.prompt).decode()
            workspace=sum(count(read_artifact(path.parent,item.artifact).decode()) for item in task.workspace)
            for arm in manifest.arms:
                memory=read_artifact(path.parent,arm.memory_pack).decode()
                controller=object.__new__(Controller)
                controller.request={'controller_model':manifest.controller_model,'seed':manifest.seed,'prompt':prompt,'memory_pack':memory}
                controller.usage=Usage();controller._budget=manifest.controller_budget.model_dump()
                body=controller._body(controller._messages(),manifest.controller_budget.output_tokens)
                rendered=template.render(messages=body['messages'],tools=body['tools'],add_generation_prompt=True)
                tokens=count(rendered)
                rows.append({'family':path.parent.name,'task':task.id,'arm':arm.id,'initial_tokens':tokens,'workspace_tokens':workspace,'pack_tokens':count(memory),'body_sha256':hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()})
                # The smallest captured provider prompt allowance is 204800.
                # Include all initial workspace bytes and the full output budget.
                if tokens+workspace+manifest.controller_budget.output_tokens>=204800:
                    raise ValueError('Generated pack initial request exceeds reviewed route allowance')
    if len(rows)!=96:raise ValueError('Expected 24 tasks across four generated arms')
    return {'rows':rows,'tokenizer_sha256':hashlib.sha256((assets/'tokenizer.json').read_bytes()).hexdigest(),'model_calls':0,'scope':'Initial messages/tools/schema plus complete workspace; subsequent tool history remains under the existing controller budgets.'}
