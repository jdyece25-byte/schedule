/* A date-only gift: no schedule writes, credentials, audio or external requests. */
(() => {
  'use strict';
  let dialog, frame = 0, opener, blowTimers = [], blowing = false, blowRun = 0;
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const paragraphs = [
    '생일 축하해!! 출국한 지도 벌써 한달이 넘어가네 교환 준비하고 적응하는 데에 챙길 게 너무 많았을 텐데도 벌써 룸메들이랑 잘 지내는 게 정말 대단하고 멋있어 앞으로도 유덥에서 좋은 시간만 잔뜩 보내고 무사히 돌아왔으면 좋겠다.',
    '요즘에는 학교 안, 38동이나 39동 사이, 대치동과 삼성역 거리처럼 평소에 별 생각 없이 뛰어다니던 루트를 지날 때마다 항상 마음 한구석에는 누나 생각이 나 원래 주고 싶었던 다이어리도 조금밖에 못 적었지만 차마 못 버리겠더라',
    '같이 있을 때 더 잘해줄걸 하는 후회도 있고, 함께 있을 수 있던 시간이 짧았다는 것에 대한 아쉬움도 있지만, 이제는 지금에 집중하려고 온라인 상에서는 항상 누나랑 같이 있고, 학교에서는 내 할 것에 최선을 다하면서 열심히 살게.',
    '누나랑 지금까지 예쁘게 사귈 수 있어서 너무 좋아ㅏ 누나가 내 애인인 것이 정말 큰 축복이야 항상 많이 사랑하구 금방 내년이 와서 다시 꼬옥 안을 수 있으면 좋겠다',
    '룸메들이랑 재밌는 생일파티 하구 많이많이 사랑해'
  ];
  function stop() {
    cancelAnimationFrame(frame); frame = 0;
    if (!dialog) return;
    const canvas = dialog.querySelector('canvas');
    canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
    dialog.classList.remove('birthday-intro');
    dialog.querySelector('.birthday-scroll').inert = false;
    dialog.querySelector('.birthday-skip').hidden = true;
  }
  function startIntro() {
    stop(); settleBlow(); setCandle(false);
    dialog.querySelector('.birthday-letter').hidden = true;
    dialog.querySelector('.birthday-primary').setAttribute('aria-expanded', 'false');
    dialog.querySelector('.birthday-scroll').scrollTop = 0;
    if (motion.matches || document.hidden || !dialog.open) return;
    const canvas = dialog.querySelector('canvas'), ctx = canvas.getContext('2d');
    if (!ctx) return;
    const w = dialog.clientWidth, h = dialog.clientHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = w*dpr; canvas.height = h*dpr; ctx.scale(dpr,dpr);
    dialog.classList.add('birthday-intro');
    dialog.querySelector('.birthday-scroll').inert = true;
    dialog.querySelector('.birthday-skip').hidden = false;
    // A world-space camera follows the rocket; near and far stars move at different speeds.
    const stars = Array.from({length:220}, () => ({x:Math.random()*w,y:(Math.random()*5-3)*h,
      depth:.3+Math.random()*.7,size:.4+Math.random()*1.1,phase:Math.random()*6}));
    const sparks = Array.from({length:200}, (_,i) => ({angle:i/200*Math.PI*2,
      speed:55+Math.random()*160,life:1.5+Math.random()*1.5,color:['#ffd68c','#f4b6d0','#c4b9ff','#a8ddf0'][i%4]}));
    const trail = [], started = performance.now();
    const smooth = x => { x=Math.max(0,Math.min(1,x)); return x*x*(3-2*x); };
    function render(now) {
      const t=(now-started)/1000;
      const camera=-h*1.55*smooth((t-.8)/3.6);
      const zoom=1-.15*smooth((t-4.3)/1.1);
      ctx.clearRect(0,0,w,h);
      // Fade the camera shot into the illustrated couple after the firework bloom.
      ctx.globalAlpha=1-smooth((t-6.1)/1.7);
      ctx.save();
      const sky=ctx.createLinearGradient(0,0,w,h);
      sky.addColorStop(0,'#090f2e');sky.addColorStop(.55,'#20234b');sky.addColorStop(1,'#584069');
      ctx.fillStyle=sky;ctx.fillRect(0,0,w,h);
      const paintedSky=dialog.querySelector('.birthday-photo');
      if(paintedSky.complete&&paintedSky.naturalWidth){
        ctx.globalAlpha=(1-smooth((t-6.1)/1.7))*.42;
        const skyHeight=h*1.3, skyWidth=skyHeight*1024/440;
        ctx.drawImage(paintedSky,0,0,1024,440,(w-skyWidth)/2,-h*.22-camera*.08,skyWidth,skyHeight);
        ctx.globalAlpha=1-smooth((t-6.1)/1.7);
      }
      ctx.translate(w/2,h*.35);ctx.scale(zoom,zoom);ctx.translate(-w/2,-h*.35);
      for(const star of stars){
        const y=star.y-camera*star.depth;
        if(y<-20||y>h+20)continue;
        ctx.fillStyle=`rgba(244,229,206,${.35+.3*Math.sin(t*.8+star.phase)**2})`;
        ctx.beginPath();ctx.arc(star.x,y,star.size,0,Math.PI*2);ctx.fill();
      }
      // Broad translucent cloud bands pass below the rising camera.
      for(let i=0;i<5;i++){
        const y=h*(.2+i*.32)-camera*.65;
        const cloud=ctx.createRadialGradient(w*(i%2?.8:.2),y,0,w*.5,y,w*.8);
        cloud.addColorStop(0,'#9b7aa41f');cloud.addColorStop(1,'#9b7aa400');
        ctx.fillStyle=cloud;ctx.fillRect(-w,y-h*.15,w*3,h*.3);
      }
      for(let layer=0;layer<3;layer++){
        const y=h*(.73+layer*.09)-camera*(.75+layer*.12);
        ctx.fillStyle=['#292b4a','#181e38','#10162c'][layer];ctx.beginPath();ctx.moveTo(-w,h*4);
        for(let i=-1;i<=12;i++)ctx.lineTo(i*w/10,y-Math.sin(i*1.6+layer)*h*.05-Math.cos(i*.7)*h*.04);
        ctx.lineTo(w*2,h*4);ctx.fill();
      }
      const progress=Math.max(0,Math.min(1,(t-.35)/4));
      const rocket={x:w*(.5+Math.sin(progress*Math.PI*1.5)*.16),y:h*(.92-2.22*progress)};
      if(t>.35&&t<4.35){
        trail.push({...rocket,at:t});
        while(trail.length>75||trail[0]?.at<t-.65)trail.shift();
        for(let i=1;i<trail.length;i++){
          const p=trail[i],q=trail[i-1];ctx.strokeStyle=`rgba(255,211,132,${i/trail.length*.8})`;
          ctx.lineWidth=1+i/trail.length*2;ctx.beginPath();ctx.moveTo(q.x,q.y-camera);ctx.lineTo(p.x,p.y-camera);ctx.stroke();
          if(i%3===0){
            ctx.fillStyle=`rgba(255,218,151,${i/trail.length*.7})`;
            ctx.beginPath();ctx.arc(p.x+Math.sin(i*2.4+t*3)*9,p.y-camera+(t-p.at)*28,1.2,0,Math.PI*2);ctx.fill();
          }
        }
        const y=rocket.y-camera;
        const glow=ctx.createRadialGradient(rocket.x,y,0,rocket.x,y,24);
        glow.addColorStop(0,'#fff4ce');glow.addColorStop(.15,'#ffdb92cc');glow.addColorStop(1,'#ffbd6000');
        ctx.fillStyle=glow;ctx.fillRect(rocket.x-24,y-24,48,48);
        ctx.fillStyle='#fff7df';ctx.beginPath();ctx.arc(rocket.x,y,2.8,0,Math.PI*2);ctx.fill();
      }
      // Secondary blooms stagger around the tracked rocket's final position.
      for(let burst=0;burst<3;burst++){
        const age=t-4.35-burst*.48;
        if(age<0)continue;
        const bx=w*(burst===0?.34:burst===1?.78:.15),by=-h*1.3-camera+burst*h*.11;
        for(const p of sparks){
          if(age>p.life)continue;
          const vx=Math.cos(p.angle)*p.speed,vy=Math.sin(p.angle)*p.speed;
          const scale=Math.min(w/390,1.5),a=age*scale;
          const x=bx+vx*a,y=by+vy*a+30*age*age;
          ctx.globalAlpha=(1-smooth((t-6.1)/1.7))*Math.max(0,1-age/p.life);
          ctx.globalCompositeOperation='lighter';ctx.strokeStyle=p.color;ctx.lineWidth=1.8;ctx.beginPath();ctx.moveTo(x-vx*.035,y-vy*.035);ctx.lineTo(x,y);ctx.stroke();
        }
      }
      ctx.restore();ctx.globalAlpha=1;
      if(t<7.8&&dialog.open&&!document.hidden)frame=requestAnimationFrame(render);
      else stop();
    }
    frame=requestAnimationFrame(render);
  }

  function setCandle(out) {
    dialog.classList.toggle('candles-out', out);
    dialog.querySelector('.birthday-candle-button').setAttribute('aria-pressed', String(out));
    dialog.querySelector('.birthday-candle-button').setAttribute('aria-label', out ? '촛불 켜기' : '촛불 끄기');
    dialog.querySelector('.birthday-blow').textContent = out ? '촛불 켜기' : '촛불 끄기';
  }
  function clearBlow() {
    blowRun++;
    blowTimers.forEach(clearTimeout); blowTimers = []; blowing = false;
    dialog.classList.remove('blowing', 'blow-wind');
    dialog.querySelector('.birthday-stage').removeAttribute('aria-busy');
    for (const selector of ['.birthday-candle-button', '.birthday-blow']) dialog.querySelector(selector).disabled = false;
  }
  function settleBlow() {
    if (blowing) setCandle(true);
    clearBlow();
  }
  async function toggleCandle() {
    if (blowing) return;
    if (dialog.classList.contains('candles-out')) { clearBlow(); setCandle(false); return; }
    stop();
    if (motion.matches) { setCandle(true); openLetter(); return; }
    blowing = true;
    const run = ++blowRun;
    dialog.querySelector('.birthday-stage').setAttribute('aria-busy', 'true');
    for (const selector of ['.birthday-candle-button', '.birthday-blow']) dialog.querySelector(selector).disabled = true;
    const pose = dialog.querySelector('.birthday-pose');
    // The extra pose loads when the dialog opens; a missing asset never traps the controls.
    if (!pose.complete || !pose.naturalWidth) {
      setCandle(true); clearBlow(); openLetter(); return;
    }
    dialog.classList.add('blowing');
    const later = (ms, action) => blowTimers.push(setTimeout(() => {
      if (run === blowRun && dialog.open) action();
    }, ms));
    later(650, () => dialog.classList.add('blow-wind'));
    later(1350, () => setCandle(true));
    later(2600, () => { clearBlow(); openLetter(); });
  }

  function openLetter() {
    settleBlow(); stop();
    if (!dialog.open) return;
    const letter = dialog.querySelector('.birthday-letter');
    letter.hidden = false;
    dialog.querySelector('.birthday-primary').setAttribute('aria-expanded', 'true');
    letter.focus({preventScroll:true});
    letter.scrollIntoView({behavior:motion.matches?'auto':'smooth', block:'start'});
  }

  function create() {
    dialog = document.createElement('dialog');
    dialog.className = 'birthday-dialog';
    dialog.setAttribute('aria-label', '생일 축하');
    dialog.innerHTML = `<button class="birthday-close" type="button" aria-label="생일 축하 닫기" autofocus>×</button>
      <canvas class="birthday-confetti" aria-hidden="true"></canvas>
      <button class="birthday-skip" type="button" aria-label="오프닝 건너뛰기" hidden><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m7 5 8 7-8 7M18 5v14"/></svg></button>
      <div class="birthday-scroll"><section class="birthday-hero">
        <div class="birthday-stage">
          <div class="birthday-scene">
            <img class="birthday-photo" src="birthday-night.webp?v=20261020-6" width="1024" height="1536" alt="은하수 아래 남녀가 함께 케이크를 바라보는 모습" decoding="async">
            <img class="birthday-pose" src="birthday-blow.webp?v=20261020-6" width="1024" height="1536" alt="" aria-hidden="true" decoding="async">
            <svg class="birthday-breath" viewBox="0 0 1024 1536" aria-hidden="true"><path d="M609 892 Q574 930 508 979"/><path d="M617 900 Q585 940 516 990"/><path d="M605 888 Q563 916 503 971"/></svg>
            <div class="birthday-candle-glow" aria-hidden="true"></div>
            <button class="birthday-candle-button" type="button" aria-label="촛불 끄기" aria-pressed="false">
              <span class="birthday-wax" aria-hidden="true"></span><span class="birthday-wick" aria-hidden="true"></span>
              <span class="birthday-flame" aria-hidden="true"></span><span class="birthday-smoke" aria-hidden="true"></span>
            </button>
          </div>
        </div>
        <div class="birthday-actions">
          <button class="birthday-blow" type="button">촛불 끄기</button>
          <button class="birthday-primary" type="button" aria-expanded="false" aria-controls="birthday-letter"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/></svg>편지</button>
          <button class="birthday-replay" type="button" aria-label="애니메이션 다시 보기"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 10a8 8 0 1 1 1 8M4 4v6h6"/></svg></button>
        </div>
      </section>
      <article class="birthday-letter" id="birthday-letter" hidden aria-label="편지" tabindex="-1">
        <div class="birthday-letter-body"></div><p class="birthday-sign">-남친이</p>
      </article></div>`;
    for (const text of paragraphs) {
      const p = document.createElement('p'); p.textContent = text;
      dialog.querySelector('.birthday-letter-body').appendChild(p);
    }
    document.body.appendChild(dialog);
    dialog.querySelector('.birthday-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { clearBlow(); stop(); if (opener?.isConnected) opener.focus(); });
    dialog.querySelector('.birthday-candle-button').addEventListener('click', toggleCandle);
    dialog.querySelector('.birthday-blow').addEventListener('click', toggleCandle);
    dialog.querySelector('.birthday-replay').addEventListener('click', startIntro);
    dialog.querySelector('.birthday-skip').addEventListener('click', () => {
      stop(); dialog.querySelector('.birthday-candle-button').focus({preventScroll:true});
    });
    dialog.querySelector('.birthday-primary').addEventListener('click', openLetter);
    document.addEventListener('visibilitychange', () => { if (document.hidden) { const pending=blowing; settleBlow(); stop(); if(pending) openLetter(); } });
    motion.addEventListener('change', () => { const pending=blowing; settleBlow(); stop(); if(pending) openLetter(); });
    window.addEventListener('resize', stop);
  }
  window.ScheduleBirthday = {
    open() {
      if (!dialog) create();
      if (dialog.open) return;
      opener = document.activeElement;
      clearBlow(); setCandle(false);
      dialog.querySelector('.birthday-letter').hidden = true;
      dialog.querySelector('.birthday-primary').setAttribute('aria-expanded', 'false');
      dialog.showModal();
      dialog.querySelector('.birthday-scroll').scrollTop = 0;
      startIntro();
    }
  };
})();
