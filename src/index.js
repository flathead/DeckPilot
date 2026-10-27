/** DeckPilot 0.2.0: based on proilyxa/deck-pc-on-off (BSD-3-Clause). */
const R = globalThis.SP_REACT;
const U = globalThis.DFL;
const connect = globalThis.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit?.connect;
if (!R?.createElement || !R.useState || !U?.PanelSection || !connect) {
  throw new Error('DeckPilot: обновите Decky Loader: React/UI API недоступен');
}
const api = connect(2, 'DeckPilot');
const call = (name, ...args) => api.call(name, ...args);
const h = R.createElement;
const {useState, useEffect, useRef} = R;
const {PanelSection, PanelSectionRow, ButtonItem, DropdownItem, TextField,
  ConfirmModal, showModal, staticClasses} = U;
const theme = {space:8, gap:16, muted:.85, error:'#ffc09e', success:'#77d5a0',
  surface:'rgba(255,255,255,.06)', radius:8};
const hint = {fontSize:'.88em', opacity:theme.muted, lineHeight:1.45};
const notice = {padding:theme.gap, borderRadius:theme.radius, background:theme.surface, lineHeight:1.45};
const mono = {fontFamily:'monospace', overflowWrap:'anywhere', fontSize:'.85em'};
const text = value => h('div', {style:hint}, value);
const row = (key, child) => h(PanelSectionRow, {key}, child);
const toast = (title, body) => api.toaster.toast({title,body});
const errorText = e => String(e?.message || e);
const blank = host => ({id:host?.id||'', name:host?.name||'', ip:host?.ip||'',
  mac:host?.mac||'', ssh_port:String(host?.ssh_port||22)});
const headlessExplanation = 'Со Steam Headless вы играете на Deck через Steam Remote Play / Steam Link, а основной рабочий стол ПК остаётся свободным. Для этого на Linux-ПК нужна отдельная игровая сессия Steam в контейнере. Через DeckPilot вы сможете включать и останавливать эту сессию.';
const phases = {stopped:'Выключен', ready:'Steam готов', starting:'Запускается',
  stopping:'Останавливается', running:'Контейнер запущен', exited:'Остановлен',
  degraded:'Требуется проверка на ПК', failed:'Ошибка', paused:'Приостановлен'};

function Content() {
  const [hosts,setHosts] = useState([]);
  const [selected,setSelected] = useState('');
  const [screen,setScreen] = useState('loading');
  const [step,setStep] = useState(0);
  const [os,setOs] = useState('linux');
  const [wantsHeadless,setWantsHeadless] = useState(null);
  const [ip,setIp] = useState('');
  const [pin,setPin] = useState('');
  const [found,setFound] = useState([]);
  const [info,setInfo] = useState(null);
  const [busy,setBusy] = useState('');
  const [error,setError] = useState('');
  const [online,setOnline] = useState(null);
  const [headless,setHeadless] = useState(null);
  const [steamPin,setSteamPin] = useState('');
  const [pairOpen,setPairOpen] = useState(false);
  const [form,setForm] = useState(blank());
  const [details,setDetails] = useState(false);
  const [feedback,setFeedback] = useState('');
  const currentId = useRef('');
  const live = useRef(true);
  const active = hosts.find(x=>String(x.id)===selected);
  const showHeadless = !!(active?.show_headless && active?.headless);
  useEffect(()=>{currentId.current=selected;setFeedback('');},[selected]);
  useEffect(()=>{live.current=true;return()=>{live.current=false;};},[]);
  async function load(prefer, navigate=true) {
    const value = await call('get_hosts');
    const list = Array.isArray(value) ? value : [];
    if (!live.current) return;
    setHosts(list);
    const id = String(prefer || currentId.current);
    setSelected(list.some(x=>String(x.id)===id)?id:String(list[0]?.id||''));
    if(navigate) setScreen(list.length?'home':'wizard');
    return list;
  }
  useEffect(()=>{load('').catch(e=>{setError(errorText(e));setScreen('wizard');});},[]);
  // Serialize polls, ignore old-host results, and pause during lifecycle operations.
  useEffect(()=>{
    let disposed=false, timer;
    setOnline(null);setHeadless(null);setPairOpen(false);setSteamPin('');
    if(!selected || screen!=='home' || busy) return;
    async function poll(){
      try {
        const status=await call('host_status',selected);
        if(disposed) return;
        setOnline(status.success ? !!status.ssh_available : null);
        if(status.ssh_available && showHeadless){
          const state=await call('headless_action',selected,'status');
          if(!disposed) setHeadless(state.success?state.data:{error:state.error});
        }
      }catch {if(!disposed)setOnline(null);}
      if(!disposed) timer=setTimeout(poll,15000);
    }
    poll();return ()=>{disposed=true;clearTimeout(timer);};
  },[selected, screen, busy, showHeadless]);
  async function perform(key, work){
    if(busy) return;
    setBusy(key);setError('');setFeedback('');
    try {await work();} catch(e){if(live.current)setError(errorText(e));}
    finally {if(live.current)setBusy('');}
  }
  function wizard(){setScreen('wizard');setStep(0);setWantsHeadless(null);
    setIp('');setPin('');setInfo(null);setFound([]);setError('');setFeedback('');}
  async function probe(address){
    const result=await call('pairing_info',address.trim());
    if(!result.success) throw new Error(result.error);
    setIp(result.host.ip);setInfo(result.host);setStep(3);
  }
  async function scan(){
    const result=await call('discover_pairing_hosts');
    if(!result.success) throw new Error(result.error);
    setFound(result.hosts||[]);
    if(result.hosts?.length===1) await probe(result.hosts[0].ip);
    else if(!result.hosts?.length) throw new Error('ПК не найден. Оставьте мастер на ПК открытым и введите IP ниже. Проверьте, что оба устройства в одной сети.');
  }
  async function pair(){
    const result=await call('pair_host',info.ip,pin,info.fingerprint,wantsHeadless===true);
    if(!result.success) throw new Error(result.error);
    await load(result.host.id);setPin('');
    toast('DeckPilot','ПК подключён');setFeedback('ПК подключён. Можно включать и выключать его с Deck.');
    if(wantsHeadless && !result.host.headless)
      setError('ПК подключён. Headless не обнаружен: выберите интеграцию в мастере на ПК, затем добавьте управление в настройках.');
  }
  function confirm(title, description, label, work){
    showModal(h(ConfirmModal,{strTitle:title,strDescription:description,
      strOKButtonText:label,strCancelButtonText:'Отмена',onOK:()=>perform(label,work)}));
  }
  async function power(action){
    const result=await call(action==='wake'?'wake_host':'shutdown_host',active.id);
    if(!result.success) throw new Error(result.error);
    toast('DeckPilot',result.message);
    setFeedback(action==='wake'?'Отправили сигнал пробуждения. Дождитесь связи с ПК.':'Отправили команду выключения. Проверьте завершение работы на ПК.');
  }
  async function headlessCommand(action){
    const result=await call('headless_action',active.id,action,action==='pair'?steamPin:'');
    if(!result.success) throw new Error(result.error);
    if(action==='pair'){setSteamPin('');toast('Steam Link','Проверьте подключение на Deck.');setFeedback('Передали PIN в Steam. Проверьте подключение на Deck.');}
    else {setHeadless(result.data);if(action==='start'||action==='stop')setFeedback(action==='start'?'Запрос на запуск Headless выполнен. Готовность сессии показана ниже.':'Запрос на остановку Headless выполнен. ПК продолжает работу.');}
  }
  async function test(){
    const result=await call('test_connection',active.id);
    if(!result.success) throw new Error(result.error);
    await load(active.id,false);toast('DeckPilot',result.message);setFeedback('Проверили вход по SSH. Ключи работают.');
  }
  async function visibility(enabled){
    const result=await call('set_headless_visibility',active.id,enabled);
    if(!result.success) throw new Error(result.error);
    await load(active.id,false);
    if(enabled)setScreen('home');
  }
  const rows=[];
  function add(key, child){rows.push(row(key,child));}
  function note(key, value){add(key,text(value));}
  function button(key,title,work,disabled=false){
    add(key,h(ButtonItem,{layout:'below',bottomSeparator:'none',highlightOnFocus:true,disabled:!!busy||disabled,onClick:work},title));
  }
  function input(key,title,value,onChange,extra={}){
    add(key,h(TextField,{label:title,value,disabled:!!busy,onChange:e=>onChange(e.target.value),...extra}));
  }
  function heading(value){add('heading',h('div',{role:'heading','aria-level':2,style:{fontSize:'1.05em',fontWeight:600,padding:'16px 0 4px'}},value));}
  if(screen==='loading') note('loading','Загрузка…');
  if(screen==='wizard'){
    heading(`Подключение ПК · шаг ${step+1} из 4`);
    if(step===0){
      note('welcome','Подключите домашний ПК, чтобы включать и выключать его с Deck. Мастер создаст и сохранит ключи доступа.');
      add('os',h(DropdownItem,{label:'Система на компьютере',selectedOption:os,
        rgOptions:[{data:'linux',label:'Linux'},{data:'windows',label:'Windows 10 / 11'}],
        onChange:item=>setOs(item.data)}));
      button('next','Продолжить',()=>{setStep(1);setError('');});
      if(!hosts.length)button('import','Перенести ПК из PC On/Off',()=>perform('import',async()=>{
        const result=await call('import_legacy');if(!result.success)throw new Error(result.error);
        await load('');toast('DeckPilot',`Перенесено ПК: ${result.count}`);
      }));
    }else if(step===1){
      note('explain',headlessExplanation);
      note('installed','Steam Headless уже установлен на этом компьютере?');
      button('no','Нет · только питание ПК',()=>{setWantsHeadless(false);setStep(2);});
      if(os==='linux')button('yes','Да · подключить управление',()=>{setWantsHeadless(true);setStep(2);});
      else note('windows-headless','Интеграция Linux-контейнера и Ambxst доступна на Linux. На Windows доступны WoL, проверка подключения и выключение.');
    }else if(step===2){
      add('host-setup',h('div',{style:notice},
        h('div',null,os==='linux'?'На ПК откройте комплект DeckPilot Host и запустите setup-linux.sh. Он сам запросит sudo.':'На ПК распакуйте DeckPilot Host и дважды нажмите Setup-Windows.cmd. Подтвердите запрос администратора.'),
        os==='linux'?h('code',{style:{display:'block',marginTop:8}},'bash setup-linux.sh'):null,
        h('div',{style:{marginTop:8}},'Мастер покажет PIN. Оставьте окно открытым.'),
        wantsHeadless?h('div',{style:{marginTop:8}},'В мастере на ПК выберите установленный Steam Headless: мод Ambxst или Docker.'):null));
      button('find',busy==='scan'?'Ищу ПК…':'Мастер на ПК открыт · найти ПК',()=>perform('scan',scan));
      for(const host of found) button('found-'+host.ip,`${host.name} · ${host.ip}`,()=>perform('probe',()=>probe(host.ip)));
      input('ip','IP компьютера, если поиск не помог',ip,v=>{setIp(v);setInfo(null);});
      button('probe','Проверить адрес',()=>perform('probe',()=>probe(ip)),!ip.trim());
    }else if(step===3 && info){
      note('target',`${info.name} · ${info.ip}`);
      input('pin','PIN из мастера на ПК · 8 цифр',pin,v=>setPin(v.replace(/\D/g,'').slice(0,8)),{inputMode:'numeric'});
      note('instruction','После нажатия «Подключить» на ПК появится код сверки. Сравните его с этим кодом и подтвердите запрос на ПК.');
      add('verification',h('div',{style:{...notice,fontFamily:'monospace',fontVariantNumeric:'tabular-nums',fontSize:'1.25em',letterSpacing:.5,textAlign:'center'}},info.verification));
      if(busy==='pair')add('waiting',h('div',{role:'status',style:notice},'Ожидаю подтверждения на ПК. Коды совпали? Введите YES в его окне.'));
      button('pair',busy==='pair'?'Ожидаю ПК…':'Подключить',()=>perform('pair',pair),!/^\d{8}$/.test(pin));
      note('trust','Подключайте в домашней сети. Если коды различаются, отклоните запрос на ПК.');
    }
    if(step>0)button('back','Назад',()=>{setStep(step-1);setError('');});
    if(hosts.length)button('cancel','Вернуться к компьютерам',()=>{setScreen('home');setError('');});
  }
  if(screen==='home' && active){
    add('host',h(DropdownItem,{label:'Компьютер',selectedOption:selected,
      rgOptions:hosts.map(x=>({data:String(x.id),label:x.name})),
      disabled:!!busy,onChange:item=>{setSelected(String(item.data));setError('');}}));
    note('address',`${active.ip} · ${active.platform==='windows'?'Windows':active.platform==='linux'?'Linux':'ПК'}`);
    add('status',h('div',{role:'status',style:{...hint,color:online?theme.success:undefined}},
      online===true?'SSH доступен':online===false?'ПК недоступен по SSH':busy?'Операция выполняется…':'Проверяю доступность…'));
    button('wake',busy==='wake'?'Отправка…':'Включить ПК · Wake-on-LAN',()=>perform('wake',()=>power('wake')),!active.mac);
    if(!active.mac)note('no-mac','Для включения укажите MAC сетевой карты в настройках ПК.');
    button('shutdown','Выключить ПК',()=>confirm('Выключить компьютер?',
      `Завершить работу «${active.name}»? Сохраните игру и документы. Выключится весь ПК.`,
      'Выключить',()=>power('shutdown')),online===false);
    if(showHeadless){
      heading('Steam Headless');
      note('headless-state',headless?.error || phases[headless?.state] || (online===false?'ПК недоступен':'Проверяю сессию…'));
      if(headless?.address)note('steam-address','Адрес для Steam Link: '+headless.address);
      if(active.headless==='docker')note('docker-note','Статус относится к контейнеру. Готовность Steam проверяйте в Remote Play на Deck.');
      button('start','Запустить Headless',()=>perform('headless-start',()=>headlessCommand('start')),online!==true||headless?.running===true);
      button('stop','Остановить Headless',()=>confirm('Остановить игровую сессию?',
        active.headless==='ambxst'?'Контроллер попросит игру и Steam завершиться. ПК продолжит работу.':'Контейнер получит сигнал завершения без принудительного убийства. Сначала сохраните игру. ПК продолжит работу.',
        'Остановить',()=>headlessCommand('stop')),online!==true||headless?.running===false);
      if(active.steam_pair){
        button('pair-open','Подключить Steam Link',()=>setPairOpen(!pairOpen),online!==true||!headless?.running);
        if(pairOpen){
          note('steam-help','В Steam Link на Deck выберите этот ПК. Введите появившийся на Deck четырёхзначный PIN здесь. DeckPilot передаст его Steam в Headless.');
          input('steam-pin','PIN Steam Link · 4 цифры',steamPin,v=>setSteamPin(v.replace(/\D/g,'').slice(0,4)),{inputMode:'numeric'});
          button('steam-submit','Передать PIN в Steam',()=>perform('steam-pair',()=>headlessCommand('pair')),!/^\d{4}$/.test(steamPin));
        }
      }
    }
    button('settings','Настройки ПК',()=>{setForm(blank(active));setDetails(false);setScreen('settings');setError('');});
    button('add','Подключить ещё ПК',wizard);
  }
  if(screen==='settings' && active){
    heading('Настройки · '+active.name);
    button('test',busy==='test'?'Проверяю ключи…':'Проверить подключение и ключи',()=>perform('test',test));
    button('details',details?'Скрыть адрес и название':'Изменить адрес и название…',()=>setDetails(!details));
    if(details){for(const [key,title] of [['name','Название'],['ip','IPv4 в локальной сети'],['mac','MAC для Wake-on-LAN'],['ssh_port','Порт SSH']]){
      input(key,title,form[key],value=>setForm(old=>({...old,[key]:value})));
    }
    button('save','Сохранить',()=>perform('save',async()=>{
      const result=await call('save_host',form.id,form.name.trim(),form.ip.trim(),form.mac.trim(),form.ssh_port);
      if(!result.success)throw new Error(result.error);
      await load(result.host.id);toast('DeckPilot','Настройки сохранены');
    }));}
    if(active.show_headless)button('hide','Скрыть управление Steam Headless',()=>perform('hide',()=>visibility(false)));
    else button('enable','Добавить Steam Headless…',()=>{setScreen('headless-setup');setError('');});
    button('remove','Удалить ПК',()=>confirm('Удалить компьютер?',`Удалить «${active.name}» из DeckPilot? Доступ на ПК можно отозвать отдельно.`,
      'Удалить',async()=>{const result=await call('delete_host',active.id);if(!result.success)throw new Error(result.error);const list=await load('');if(list&&!list.length)wizard();}));
    button('back','Назад',()=>{setScreen('home');setError('');});
  }
  if(screen==='headless-setup' && active){
    heading('Добавить Steam Headless');
    note('explain',headlessExplanation);
    note('setup-help','Когда Steam Headless будет настроен на Linux-ПК, повторно запустите setup-linux.sh и выберите интеграцию. Затем проверьте её здесь. Повторное сопряжение Deck не требуется.');
    button('enable','Headless установлен · проверить и добавить',()=>perform('enable',()=>visibility(true)));
    button('back','Назад',()=>{setScreen('settings');setError('');});
  }
  const progress = {'headless-start':'Запускаю Headless. Первый запуск может занять несколько минут.',
    'Остановить':'Завершаю игровую сессию. Ожидаю закрытия игры и Steam.',
    'Выключить':'Отправляю команду выключения…',test:'Проверяю вход по SSH…',
    enable:'Проверяю подключение Headless…'};
  const message=error||progress[busy]||feedback;
  if(message)rows.splice(Math.min(2,rows.length),0,row('message',h('div',{
    role:error?'alert':'status',style:{...hint,color:error?theme.error:undefined,padding:'8px 0'}},message)));
  if(screen==='settings')note('version','DeckPilot · 0.2.0');
  return h(PanelSection,null,...rows);
}
export default function defineDeckPilot(){
  return {name:'DeckPilot',titleView:h('div',{className:staticClasses?.Title},'DeckPilot'),
    icon:h('svg',{viewBox:'0 0 24 24',width:20,height:20,fill:'none',stroke:'currentColor',strokeWidth:2,'aria-hidden':true},
      h('path',{d:'M12 2v10M6.2 5.8a9 9 0 1 0 11.6 0'})),
    content:h(Content),onDismount(){}};
}
