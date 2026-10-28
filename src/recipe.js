(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const KEY = 'schedule-chicken-soup-v1';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch {}
  let servings = saved.portionVersion === 2 && [1,2,4].includes(saved.servings) ? saved.servings : 1;
  const checked = new Set(Array.isArray(saved.checked) ? saved.checked.filter(id => ['chicken','noodles','bouillon','carrots','onion'].includes(id)) : []);
  let step = 0, deadline = 0, remaining = 0;
  const quantity = (n=servings) => ({chicken:120*n,noodles:40*n,water:n===4?'1.6L':`${400*n}mL`,oil:`${n}작은술`,carrots:{1:'½개',2:'1개',4:'2개'}[n],onion:{1:'¼개',2:'½개',4:'1개'}[n],cubes:n});
  const products = [
    {id:'chicken',name:'뼈 없는 닭다리살',brand:'Heritage Farm · Boneless & Skinless Chicken Thighs',query:'Heritage Farm boneless skinless chicken thighs',amount:n=>`${quantity(n).chicken}g`,note:'필요한 양만 사용하고 나머지는 소분 냉동. 작은 팩 총액과 $/lb를 비교하세요. 할인 닭가슴살로 대체 가능.'},
    {id:'noodles',name:'넓은 에그 누들',brand:'Kroger · Wide Egg Noodles · 12 oz',query:'Kroger wide egg noodles 12 oz',amount:n=>`${quantity(n).noodles}g`,note:'1봉지 목표 $2 이내. 집에 짧은 파스타가 있으면 구매 생략. 조리 시간은 포장 지침을 따르세요.'},
    {id:'bouillon',name:'치킨 부용 큐브',brand:'Kroger · Chicken Bouillon Cubes · 25 ct',query:'Kroger chicken bouillon cubes 25 ct',amount:n=>`${quantity(n).cubes}개부터`,note:'1병 목표 $3 이내. 1큐브당 물 약 240mL인 제품 기준. 다른 농축도라면 포장 비율을 따르고 싱겁게 시작하세요.'},
    {id:'carrots',name:'당근',brand:'브랜드 무관 · Fresh Carrots',query:'fresh carrots',amount:n=>`${quantity(n).carrots} · 약 ${40*n}g`,note:'낱개와 작은 봉지의 총액 비교. 남은 당근은 밀폐 냉장.'},
    {id:'onion',name:'노란 양파',brand:'브랜드 무관 · Yellow Onion',query:'yellow onion',amount:n=>`${quantity(n).onion} · 약 ${40*n}g`,note:'작은 양파 기준. 대용량 망 대신 낱개 구매. 남은 양파는 밀폐 냉장.'}
  ];
  function save() { try { localStorage.setItem(KEY, JSON.stringify({portionVersion:2,servings,checked:[...checked]})); } catch {} }
  function progress() { $('shopping-progress').textContent = `준비 ${checked.size} / 5`; }
  function renderIngredients() {
    $('ingredients').replaceChildren();
    products.forEach((item,index)=>{
      const card=document.createElement('article');card.className='ingredient';
      card.innerHTML=`<div class="ingredient-top"><div class="art tile-${index}" role="img" aria-label="${item.name} 예시 그림"></div><div><h3>${item.name}</h3><p class="amount">${item.amount(servings)}</p></div></div><details class="product-detail"><summary>제품·절약 팁</summary><p class="brand">${item.brand}</p><p class="buy-note">${item.note}</p></details><div class="ingredient-bottom"><a href="https://www.qfc.com/search?query=${encodeURIComponent(item.query)}" target="_blank" rel="noopener noreferrer">QFC 상품 ↗</a><label><input type="checkbox" aria-label="${item.name} 준비 완료">준비</label></div>`;
      const input=card.querySelector('input');input.checked=checked.has(item.id);
      input.addEventListener('change',()=>{if(input.checked)checked.add(item.id);else checked.delete(item.id);save();progress();});
      $('ingredients').append(card);
    });
    const q=quantity();$('pantry-amounts').textContent=`물 ${q.water} · 기름 ${q.oil} · 후추 · 소금 선택`;
    document.querySelectorAll('[data-servings]').forEach(button=>button.setAttribute('aria-pressed',String(Number(button.dataset.servings)===servings)));
    progress();
  }
  function steps() {
    const q=quantity();
    return [
      {title:'재료 썰기',visual:'당근과 양파를 도마에서 작게 써는 모습',time:0,detail:`당근 ${q.carrots} → 5mm. 양파 ${q.onion} → 잘게. 면 ${q.noodles}g, 물 ${q.water}.`,tip:'생닭은 씻지 않기. 만진 뒤 손·칼·도마 세척.'},
      {title:'채소 볶기',visual:'냄비에서 당근과 양파를 나무주걱으로 볶는 모습',time:300,detail:`기름 ${q.oil} + 양파 + 당근. 중불에서 저으며 5분.`,tip:'타면 불을 줄이고 물 1~2큰술.'},
      {title:'닭 끓이기',visual:'닭고기와 당근을 넣은 국물이 냄비에서 끓는 모습',time:900,detail:`닭 ${q.chicken}g + 물 ${q.water} + 부용 ${q.cubes}개. 끓으면 중약불, 15~20분.`,tip:'끓기 시작하면 타이머. 닭 중심 74°C / 165°F까지. 부용은 포장 희석 비율 확인.'},
      {title:'닭 찢기',visual:'깨끗한 접시에서 두 개의 포크로 익은 닭고기를 찢는 모습',time:0,detail:'익은 닭을 꺼내 포크로 찢고, 냄비에 다시 넣기.',tip:'생닭 접시 대신 깨끗한 접시 사용.'},
      {title:'면 넣기',visual:'끓는 닭고기 수프에 에그 누들을 붓는 모습',time:480,detail:`다시 끓으면 면 ${q.noodles}g. 저으며 6~8분, 포장 조리 시간 확인.`,tip:'면이 부드러워지면 불을 끄세요.'},
      {title:'간 맞추기',visual:'완성된 치킨 누들 수프 한 그릇에 후추를 뿌리는 모습',time:0,detail:`후추 약간. 싱거우면 부용 ¼개씩. 짜면 뜨거운 물.`,tip:'남으면 2시간 안에 냉장, 3~4일 안에 먹기.'}
    ];
  }
  function updateTimer() {
    if(deadline){remaining=Math.max(0,Math.ceil((deadline-Date.now())/1000));if(!remaining){deadline=0;$('timer-status').textContent='설정한 시간이 끝났어요. 음식 상태를 확인하세요.';}}
    $('timer-value').textContent=`${String(Math.floor(remaining/60)).padStart(2,'0')}:${String(remaining%60).padStart(2,'0')}`;
    $('timer-start').textContent=deadline?'일시정지':remaining===0?'다시 시작':'타이머 시작';
  }
  function resetTimer(){deadline=0;remaining=steps()[step].time;$('timer-status').textContent='앱 내 타이머 · 알람 없음';updateTimer();}
  function renderStep(focus=false) {
    const current=steps()[step];
    $('step-visual').className=`step-visual cooking-${step}`;
    $('step-visual').setAttribute('aria-label',current.visual);
    $('step-meta').textContent=`${step+1} / 6 · ${servings}인분`;
    $('step-title').textContent=current.title;$('step-body').textContent=current.detail;$('step-tip').textContent=current.tip;
    $('step-dots').replaceChildren();
    steps().forEach((_,i)=>{const button=document.createElement('button');button.textContent=String(i+1);button.setAttribute('aria-label',`${i+1}단계`);if(i===step)button.setAttribute('aria-current','step');button.addEventListener('click',()=>{step=i;renderStep(true);});$('step-dots').append(button);});
    $('previous-step').disabled=step===0;$('next-step').textContent=step===5?'처음부터 다시 보기 ↺':'다음 단계 →';
    document.querySelector('.timer').hidden=!current.time;$('timer-status').hidden=!current.time;
    resetTimer();
    if(focus){$('step-card').focus({preventScroll:true});$('step-card').scrollIntoView({block:'start'});}
  }
  function showTab(cook){$('shop').hidden=cook;$('cook').hidden=!cook;$('shop-tab').setAttribute('aria-pressed',String(!cook));$('cook-tab').setAttribute('aria-pressed',String(cook));if(cook){$('step-card').focus({preventScroll:true});$('cook').scrollIntoView({block:'start'});}else $('shop').scrollIntoView({block:'start'});}
  document.querySelectorAll('[data-servings]').forEach(button=>button.addEventListener('click',()=>{servings=Number(button.dataset.servings);save();renderIngredients();renderStep();}));
  $('shop-tab').addEventListener('click',()=>showTab(false));$('cook-tab').addEventListener('click',()=>showTab(true));$('start-cooking').addEventListener('click',()=>showTab(true));$('back-shopping').addEventListener('click',()=>showTab(false));
  $('previous-step').addEventListener('click',()=>{if(step>0){step--;renderStep(true);}});$('next-step').addEventListener('click',()=>{step=(step+1)%6;renderStep(true);});
  $('timer-start').addEventListener('click',()=>{if(deadline){updateTimer();deadline=0;}else{if(!remaining)remaining=steps()[step].time;deadline=Date.now()+remaining*1000;}$('timer-status').textContent='단계·인분 변경 시 초기화';updateTimer();});
  $('timer-reset').addEventListener('click',resetTimer);document.addEventListener('visibilitychange',updateTimer);
  const stores = {
    qfc: {name:'QFC · University Village',address:'2746 NE 45th St, Seattle, WA 98105',query:'QFC 2746 NE 45th St Seattle WA',match:'아래 Kroger·Heritage Farm 상품 검색용'},
    trader: {name:'Trader Joe’s · University District',address:'4555 Roosevelt Way NE, Seattle, WA 98105',query:'Trader Joes 4555 Roosevelt Way NE Seattle WA',match:'자체 브랜드 대안 · 에그 누들은 파스타로 대체'}
  };
  document.querySelectorAll('[data-store]').forEach(button=>button.addEventListener('click',()=>{
    const store=stores[button.dataset.store],query=encodeURIComponent(store.query);
    document.querySelectorAll('[data-store]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
    $('store-name').textContent=store.name;$('store-address').textContent=store.address;$('store-match').textContent=store.match;
    $('store-map').src=`https://www.google.com/maps?q=${query}&z=14&output=embed`;
    $('store-map').title=`${store.name} 위치 지도`;
    $('store-open-map').href=`https://www.google.com/maps/search/?api=1&query=${query}`;
    $('store-directions').href=`https://www.google.com/maps/dir/?api=1&origin=University+of+Washington+Seattle&destination=${query}&travelmode=walking`;
  }));
  renderIngredients();renderStep();setInterval(updateTimer,500);
})();
