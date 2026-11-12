"""Small frozen constraint board per tree node. No extra dependency graph."""
import hashlib
import json
from pathlib import Path
import uuid

MAX_CHARS=4096


def validate(items):
    if not isinstance(items,list) or any(not isinstance(x,str) or not x.strip() for x in items):
        raise ValueError('共同约束必须是非空字符串数组（没有共同约束时显式给 []）')
    items=list(dict.fromkeys(x.strip() for x in items))
    if sum(len(x) for x in items)>MAX_CHARS:
        raise ValueError('共同约束过长，须由父节点先整理，不能静默截断')
    return items


def path(run_dir,node_id):
    return Path(run_dir)/'branch-constraints'/(hashlib.sha256(node_id.encode()).hexdigest()+'.json')


def load(run_dir,node_id):
    p=path(run_dir,node_id)
    return json.loads(p.read_text()) if p.exists() else {'node_id':node_id,'inherited':[],'local':[]}


def combined(board):
    return validate(board.get('inherited',[])+board.get('local',[]))


def save(run_dir,node_id,board):
    values=combined(board)
    board=dict(board,version=hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()[:16])
    p=path(run_dir,node_id);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(board,ensure_ascii=False,indent=1))
    tmp.replace(p)


def set_shared(run_dir,node_id,items):
    items=validate(items);board=load(run_dir,node_id)
    board['local']=items;save(run_dir,node_id,board)


def inherit(run_dir,parent_id,child_id):
    # Copy the exact board now; no live pointer to the parent's future decisions.
    parent=load(run_dir,parent_id)
    save(run_dir,child_id,{'node_id':child_id,'inherited':combined(parent),'local':[],
                          'parent_id':parent_id,'parent_version':parent.get('version','')})


def render(run_dir,node_id):
    if not run_dir:return ''
    board=load(run_dir,node_id);items=combined(board)
    if not items:return ''
    return ('\n## 已固定共同约束（继承快照 '+board.get('version','')+'）\n'
            +'\n'.join('- '+x for x in items)
            +'\n这些约束适用于本分支；发现冲突时回报父节点，不自行改约束或等待兄弟替你决定。\n')
