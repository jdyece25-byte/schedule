(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const KEY = 'schedule-chicken-soup-v1';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch {}
  let servings = saved.servings === 2 ? 2 : 4;
  const checked = new Set(Array.isArray(saved.checked) ? saved.checked.filter(id => ['chicken','noodles','bouillon','carrots','onion'].includes(id)) : []);
  let step = 0, deadline = 0, remaining = 0;
  const products = [
    {id:'chicken',name:'뼈 없는 닭다리살',brand:'Heritage Farm · Boneless & Skinless Chicken Thighs',query:'Heritage Farm boneless skinless chicken thighs',amount:n=>`${n===4?'450g (약 1 lb)':'225g (약 ½ lb)'}`,note:'4인분 구매 목표 $6 이내. 가변 중량 팩은 총액을 확인하세요. 같은 무게의 할인 닭가슴살이 더 저렴하면 대체 가능.'},
    {id:'noodles',name:'넓은 에그 누들',brand:'Kroger · Wide Egg Noodles · 12 oz',query:'Kroger wide egg noodles 12 oz',amount:n=>`${n===4?'170g (6 oz, 12 oz 봉지의 ½)':'85g (3 oz, 12 oz 봉지의 ¼)'}`,note:'1봉지 목표 $2 이내. 집에 짧은 파스타가 있으면 구매 생략. 대체 면의 조리 시간은 포장 지침을 따르세요.'},
    {id:'bouillon',name:'치킨 부용 큐브',brand:'Kroger · Chicken Bouillon Cubes · 25 ct',query:'Kroger chicken bouillon cubes 25 ct',amount:n=>`${n===4?'우선 4개, 간을 보고 최대 6개':'우선 2개, 간을 보고 최대 3개'}`,note:'1병 목표 $3 이내. 1큐브당 물 1 US cup(약 240mL)인 제품 기준. 다른 농축도라면 포장 비율을 따르고 싱겁게 시작하세요.'},
    {id:'carrots',name:'당근',brand:'브랜드 무관 · Fresh Carrots',query:'fresh carrots',amount:n=>`${n===4?'중간 크기 2개 (약 150g)':'중간 크기 1개 (약 75g)'}`,note:'목표 $2 이내. 필요한 낱개와 가장 작은 봉지의 총액을 비교하세요. 손질된 베이비 캐럿보다 일반 당근을 먼저 비교해요.'},
    {id:'onion',name:'노란 양파',brand:'브랜드 무관 · Yellow Onion',query:'yellow onion',amount:n=>`${n===4?'작은 것 1개 (약 150g)':'작은 것 ½개 (약 75g)'}`,note:'1개 목표 $1 이내. 대용량 망을 새로 살 필요 없어요. 남은 반 개는 밀폐해서 냉장하세요.'}
  ];
  function save() { try { localStorage.setItem(KEY, JSON.stringify({servings,checked:[...checked]})); } catch {} }
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
    $('pantry-amounts').textContent=servings===4?'물 1.5L · 기름 1큰술 · 후추 · 소금 선택':'물 750mL · 기름 ½큰술 · 후추 · 소금 선택';
    document.querySelectorAll('[data-servings]').forEach(button=>button.setAttribute('aria-pressed',String(Number(button.dataset.servings)===servings)));
    progress();
  }
  function steps() {
    const big=servings===4;
    return [
      {title:'재료 썰기',time:0,detail:`당근 ${big?'2개':'1개'} → 5mm. 양파 ${big?'1개':'½개'} → 잘게. 면 ${big?'170g':'85g'}, 물 ${big?'1.5L':'750mL'} 준비.`,tip:'생닭은 씻지 않기. 만진 뒤 손·칼·도마 세척.'},
      {title:'채소 볶기',time:300,detail:`기름 ${big?'1큰술':'½큰술'} + 양파 + 당근. 중불에서 저으며 5분.`,tip:'타면 불을 줄이고 물 1~2큰술.'},
      {title:'닭 끓이기',time:900,detail:`닭 ${big?'450g':'225g'} + 물 ${big?'1.5L':'750mL'} + 부용 ${big?'4개':'2개'}. 끓으면 중약불, 15~20분.`,tip:'끓기 시작하면 타이머. 닭 중심 74°C / 165°F까지 익히기. 부용은 제품 희석 비율 확인.'},
      {title:'닭 찢기',time:0,detail:'익은 닭을 꺼내 포크로 찢고, 냄비에 다시 넣기.',tip:'생닭을 담았던 접시 대신 깨끗한 접시 사용.'},
      {title:'면 넣기',time:480,detail:`다시 끓으면 면 ${big?'170g':'85g'}. 저으며 6~8분, 포장 조리 시간 확인.`,tip:'내일 먹을 국물은 면을 넣기 전에 덜어두기.'},
      {title:'간 맞추기',time:0,detail:`후추 약간. 싱거우면 부용 조금씩 추가(총 ${big?'6개':'3개'} 이내부터). 짜면 뜨거운 물.`,tip:'남은 수프는 2시간 안에 냉장, 3~4일 안에 먹기.'}
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
