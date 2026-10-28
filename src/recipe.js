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
  function progress() { $('shopping-progress').textContent = `준비 완료 ${checked.size} / 5 · 체크는 이 기기에 저장됩니다`; }
  function renderIngredients() {
    $('ingredients').replaceChildren();
    products.forEach((item,index)=>{
      const card=document.createElement('article');card.className='ingredient';
      card.innerHTML=`<div class="ingredient-top"><div class="art tile-${index}" role="img" aria-label="${item.name} 예시 그림"></div><div><h3>${item.name}</h3><p class="brand">${item.brand}</p><p class="amount">사용량 · ${item.amount(servings)}</p></div></div><p class="buy-note">${item.note}</p><div class="ingredient-bottom"><a href="https://www.qfc.com/search?query=${encodeURIComponent(item.query)}" target="_blank" rel="noopener noreferrer">QFC 상품 찾기 ↗</a><label><input type="checkbox" aria-label="${item.name} 준비 완료">준비 완료</label></div>`;
      const input=card.querySelector('input');input.checked=checked.has(item.id);
      input.addEventListener('change',()=>{if(input.checked)checked.add(item.id);else checked.delete(item.id);save();progress();});
      $('ingredients').append(card);
    });
    $('pantry-amounts').textContent=servings===4?'물 1.5L · 식용유 1큰술(15mL) · 후추 약간 · 소금은 마지막에 필요할 때만':'물 750mL · 식용유 ½큰술(7.5mL) · 후추 약간 · 소금은 마지막에 필요할 때만';
    document.querySelectorAll('[data-servings]').forEach(button=>button.setAttribute('aria-pressed',String(Number(button.dataset.servings)===servings)));
    progress();
  }
  function steps() {
    const big=servings===4;
    return [
      {title:'재료를 작게 썰어요',time:0,detail:`당근 ${big?'2개':'1개'}를 5mm 두께로 썰고, 양파 ${big?'1개':'½개'}는 잘게 썰어요. 면 ${big?'170g':'85g'}과 물 ${big?'1.5L':'750mL'}를 준비하세요.`,tip:'채소를 먼저 손질하세요. 생닭은 씻지 말고, 만진 뒤 손·도마·칼을 씻으세요.'},
      {title:'양파와 당근을 볶아요',time:300,detail:`냄비에 식용유 ${big?'1큰술':'½큰술'}을 두르고 중불로 켜요. 양파와 당근을 넣고 5분 동안 가끔 저으며 볶아요.`,tip:'양파가 살짝 투명해지면 돼요. 타려고 하면 불을 줄이고 물 1~2큰술을 넣으세요.'},
      {title:'닭을 넣고 푹 끓여요',time:900,detail:`닭 ${big?'450g':'225g'}, 물 ${big?'1.5L':'750mL'}, 부용 ${big?'4개':'2개'}를 넣어요. 한 번 끓으면 중약불로 낮추고 뚜껑을 살짝 열어 15~20분 뭉근하게 끓여요.`,tip:'타이머는 끓기 시작한 뒤 누르세요. 닭의 가장 두꺼운 곳이 74°C / 165°F가 될 때까지 추가로 익히세요. 시간만으로 익힘을 판단하지 마세요.'},
      {title:'닭고기를 찢어요',time:0,detail:'익은 닭을 깨끗한 접시에 꺼내요. 포크 두 개로 한입 크기로 찢은 뒤 냄비에 다시 넣어요.',tip:'뜨거우니 손 대신 포크를 사용하세요. 닭을 만졌던 생고기 접시를 그대로 사용하지 마세요.'},
      {title:'면을 넣고 6~8분',time:480,detail:`국물이 다시 끓으면 면 ${big?'170g':'85g'}을 넣고 가끔 저어요. 포장에 적힌 시간만큼, 보통 6~8분 익혀요. 한 가닥 먹어 보고 부드러우면 끝이에요.`,tip:'내일 먹을 몫은 면을 넣기 전에 국물을 덜어 두세요. 다른 파스타를 쓴다면 그 포장 조리 시간을 따르세요.'},
      {title:'간을 맞추면 완성',time:0,detail:`후추를 조금 넣고 맛봐요. 싱거우면 부용을 조금씩 더 넣되 총 ${big?'6개':'3개'} 이내로 먼저 맞춰 보세요. 짜면 뜨거운 물을 조금씩 넣어 조절해요.`,tip:'부용 자체가 짭짤해서 소금은 안 넣어도 될 수 있어요. 남은 수프는 2시간 안에 나눠 냉장하세요.'}
    ];
  }
  function updateTimer() {
    if(deadline){remaining=Math.max(0,Math.ceil((deadline-Date.now())/1000));if(!remaining){deadline=0;$('timer-status').textContent='설정한 시간이 끝났어요. 음식 상태를 확인하세요.';}}
    $('timer-value').textContent=`${String(Math.floor(remaining/60)).padStart(2,'0')}:${String(remaining%60).padStart(2,'0')}`;
    $('timer-start').textContent=deadline?'일시정지':remaining===0?'다시 시작':'타이머 시작';
  }
  function resetTimer(){deadline=0;remaining=steps()[step].time;$('timer-status').textContent='화면을 다시 열면 남은 시간이 갱신됩니다. 휴대폰 알람은 울리지 않아요.';updateTimer();}
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
  $('timer-start').addEventListener('click',()=>{if(deadline){updateTimer();deadline=0;}else{if(!remaining)remaining=steps()[step].time;deadline=Date.now()+remaining*1000;}$('timer-status').textContent='단계나 인분을 바꾸면 타이머가 초기화됩니다.';updateTimer();});
  $('timer-reset').addEventListener('click',resetTimer);document.addEventListener('visibilitychange',updateTimer);
  renderIngredients();renderStep();setInterval(updateTimer,500);
})();
