import assert from 'node:assert/strict';
let slots=[], cursor=0, pending=[], cleanups=[], dirty=false, tree;
let hostList=[], capabilities=false;
const calls=[];
const host={id:'pc1',name:'Game PC',ip:'192.168.1.10',mac:'00:11:22:33:44:55',ssh_port:22,platform:'linux',headless:'',show_headless:false,steam_pair:false};
const api={toaster:{toast(){}},async call(name,...args){
  calls.push([name,...args]);
  if(name==='get_hosts')return hostList.map(x=>({...x}));
  if(name==='discover_pairing_hosts')return {success:true,hosts:[{ip:host.ip,name:host.name}]};
  if(name==='pairing_info')return {success:true,host:{...host,fingerprint:'SHA256:abc',verification:'ABCD-1234-EF56'}};
  if(name==='pair_host'){
    hostList=[{...host,headless:capabilities?'ambxst':'',steam_pair:capabilities,show_headless:capabilities&&args[3]}];
    return {success:true,host:hostList[0]};
  }
  if(name==='host_status')return {success:true,ssh_available:true};
  if(name==='delete_host'){hostList=[];return {success:true};}
  if(name==='headless_action')return {success:true,data:args[1]==='start'?{state:'ready',running:true}:{state:'stopped',running:false}};
  if(name==='set_headless_visibility'){
    if(args[1]&&!capabilities)return {success:false,error:'Сначала настройте Steam Headless на ПК'};
    hostList[0]={...hostList[0],headless:capabilities?'ambxst':'',steam_pair:capabilities,show_headless:args[1]};
    return {success:true,host:hostList[0]};
  }
  throw new Error('Unexpected call: '+name);
}};
const R={
  createElement(type,props,...children){return {type,props:props||{},children};},
  useState(initial){const key=cursor++;if(!(key in slots))slots[key]=initial;
    return [slots[key],value=>{slots[key]=typeof value==='function'?value(slots[key]):value;dirty=true;}];},
  useRef(initial){const key=cursor++;if(!(key in slots))slots[key]={current:initial};return slots[key];},
  useEffect(callback,deps){const key=cursor++;const old=slots[key];
    if(!old || deps.some((x,i)=>x!==old[i])){slots[key]=deps;pending.push(()=>{cleanups[key]?.();cleanups[key]=callback();});}}
};
globalThis.SP_REACT=R;
globalThis.DFL=Object.fromEntries(['PanelSection','PanelSectionRow','ButtonItem','DropdownItem','TextField','ConfirmModal'].map(x=>[x,x]));
globalThis.DFL.showModal=modal=>modal.props.onOK();
globalThis.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit={connect:(v,name)=>{assert.equal(name,'DeckPilot');return api;}};
// Poll timers are controlled; the harness renders on demand.
const originalSetTimeout=globalThis.setTimeout;
globalThis.setTimeout=()=>0;
const {default:plugin}=await import('../dist/index.js');
const Content=plugin().content.type;
function render(){cursor=0;dirty=false;tree=Content();const effects=pending;pending=[];for(const effect of effects)effect();}
async function flush(){for(let i=0;i<16;i++){await Promise.resolve();if(dirty)render();}}
function nodes(node=tree){if(!node||typeof node!=='object')return [];return [node,...node.children.flatMap(nodes)];}
function strings(node){if(node==null)return '';if(typeof node!=='object')return String(node);return node.children.map(strings).join(' ');}
function button(title){const value=nodes().find(n=>n.type==='ButtonItem'&&strings(n)===title);assert.ok(value,'Missing button: '+title);return value;}
async function click(title){const b=button(title);assert.ok(!b.props.disabled,'Disabled: '+title);b.props.onClick();await flush();}
async function field(label,value){const item=nodes().find(n=>n.type==='TextField'&&n.props.label===label);assert.ok(item,'Missing field: '+label);item.props.onChange({target:{value}});await flush();}
async function mount(){for(const cleanup of cleanups)cleanup?.();slots=[];cleanups=[];pending=[];dirty=false;render();await flush();}
await mount();
assert.match(strings(tree),/шаг 1 из 4/);
await click('Продолжить');
assert.match(strings(tree),/отдельная игровая сессия/);
await click('Нет · только питание ПК');
await click('Мастер на ПК открыт · найти ПК');
assert.match(strings(tree),/ABCD-1234-EF56/);
assert.ok(button('Подключить').props.disabled);
await field('PIN из мастера на ПК · 8 цифр','1234abcd5678');
await click('Подключить');
assert.equal(calls.find(x=>x[0]==='pair_host')[4],false);
assert.ok(button('Включить ПК · Wake-on-LAN'));
assert.ok(!nodes().some(n=>n.type==='ButtonItem'&&/Headless|Steam Link/.test(strings(n))),'Headless controls must be hidden');
await click('Настройки ПК');await click('Добавить Steam Headless…');
assert.match(strings(tree),/Повторное сопряжение Deck не требуется/);
await click('Headless установлен · проверить и добавить');
assert.match(strings(tree),/Сначала настройте/);
capabilities=true;
await click('Headless установлен · проверить и добавить');
assert.ok(button('Запустить Headless'));
await click('Запустить Headless');
assert.ok(calls.some(x=>x[0]==='headless_action'&&x[2]==='start'));
await click('Настройки ПК');await click('Скрыть управление Steam Headless');await click('Назад');
assert.ok(!nodes().some(n=>n.type==='ButtonItem'&&/Headless|Steam Link/.test(strings(n))));
await click('Настройки ПК');await click('Удалить ПК');
assert.match(strings(tree),/шаг 1 из 4/,'Deleting the last PC must restart onboarding');
// Windows onboarding offers PC power only, with the correct launcher.
hostList=[];await mount();
const dropdown=nodes().find(n=>n.type==='DropdownItem');dropdown.props.onChange({data:'windows'});await flush();
await click('Продолжить');
assert.ok(!nodes().some(n=>n.type==='ButtonItem'&&strings(n)==='Да · подключить управление'));
await click('Нет · только питание ПК');assert.match(strings(tree),/Setup-Windows.cmd/);
for(const cleanup of cleanups)cleanup?.();globalThis.setTimeout=originalSetTimeout;
console.log('Frontend: first setup, hidden controls, later opt-in, failed enable, Windows flow: passed');
