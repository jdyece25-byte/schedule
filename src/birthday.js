/* A date-only gift: no schedule writes, credentials, audio or external requests. */
(() => {
  'use strict';
  let dialog, frame = 0, opener;
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
    const canvas = dialog?.querySelector('canvas');
    canvas?.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
  }
  function celebrate() {
    stop();
    if (motion.matches || document.hidden || !dialog.open) return;
    const canvas = dialog.querySelector('canvas'), ctx = canvas.getContext('2d');
    if (!ctx) return;
    const width = dialog.clientWidth, height = dialog.clientHeight;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = width * ratio; canvas.height = height * ratio; ctx.scale(ratio, ratio);
    const colors = ['#efd6a0', '#c1cedd', '#edd9c7', '#c8d8eb', '#fff0c6'];
    const particles = [], rockets = [];
    // Timed launches and warm, irregular falling sparks over the night sky.
    for (let i = 0; i < 10; i++) {
      rockets.push({at:i < 10 ? i*.65 : 7+(i-10)*.13,
        x:width*(.12+Math.random()*.76), y:height*(.08+Math.random()*.32),
        color:colors[i%colors.length], fired:false});
    }
    const started = performance.now();
    let previous = started;
    function burst(r) {
      for (let i = 0; i < 70; i++) {
        const angle = i/70*Math.PI*2 + Math.random()*.04;
        const speed = (55+Math.random()*90) * Math.min(width/390, 1.5);
        particles.push({x:r.x,y:r.y,vx:Math.cos(angle)*speed,vy:Math.sin(angle)*speed,
          life:2.3+Math.random()*.8,age:0,color:r.color,size:1.2+Math.random()*1.2});
      }
    }
    function draw(now) {
      const elapsed = (now-started)/1000, dt = Math.min((now-previous)/1000,.04);
      previous = now;
      ctx.clearRect(0,0,width,height);
      ctx.globalCompositeOperation = 'lighter';
      for (const r of rockets) {
        const age = elapsed-r.at;
        if (age >= 0 && age < .7) {
          const t = age/.7, y = height-(height-r.y)*(1-(1-t)**2);
          const trail = ctx.createLinearGradient(r.x,y,r.x,y+65);
          trail.addColorStop(0,r.color);trail.addColorStop(1,'transparent');
          ctx.strokeStyle=trail;ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(r.x,y);ctx.lineTo(r.x,y+65);ctx.stroke();
          ctx.fillStyle='#fff6db';ctx.beginPath();ctx.arc(r.x,y,2.5,0,Math.PI*2);ctx.fill();
        }
        if(age>=.7&&!r.fired){r.fired=true;burst(r);}
      }
      for(let i=particles.length-1;i>=0;i--){
        const p=particles[i];p.age+=dt;
        if(p.age>p.life){particles.splice(i,1);continue;}
        const oldX=p.x,oldY=p.y;
        p.x+=p.vx*dt;p.y+=p.vy*dt;p.vy+=(p.paper?10:42)*dt;
        if(!p.paper){p.vx*=Math.exp(-.5*dt);p.vy*=Math.exp(-.16*dt);}
        ctx.globalAlpha=Math.min(1,(p.life-p.age)*1.5);
        ctx.fillStyle=p.color;ctx.strokeStyle=p.color;
        if(p.paper){
          ctx.globalCompositeOperation='source-over';ctx.save();ctx.translate(p.x,p.y);
          ctx.rotate(p.spin+p.age*2);ctx.scale(Math.cos(p.age*4+p.spin),1);
          ctx.fillRect(-p.size/2,-p.size,p.size,p.size*2);ctx.restore();
        }else{
          ctx.globalCompositeOperation='lighter';ctx.lineWidth=p.size;
          ctx.beginPath();ctx.moveTo(oldX-p.vx*.035,oldY-p.vy*.035);ctx.lineTo(p.x,p.y);ctx.stroke();
          ctx.globalAlpha*=.06;ctx.beginPath();ctx.arc(p.x,p.y,p.size*3,0,Math.PI*2);ctx.fill();
        }
      }
      ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';
      if(elapsed<13.2&&dialog.open&&!document.hidden)frame=requestAnimationFrame(draw);
      else stop();
    }
    frame=requestAnimationFrame(draw);
  }

  function create() {
    dialog = document.createElement('dialog');
    dialog.className = 'birthday-dialog';
    dialog.setAttribute('aria-label', '생일 축하');
    dialog.innerHTML = `<button class="birthday-close" type="button" aria-label="생일 축하 닫기" autofocus>×</button>
      <canvas class="birthday-confetti" aria-hidden="true"></canvas>
      <div class="birthday-scroll"><section class="birthday-hero">
        <div class="birthday-stage">
          <div class="birthday-scene">
            <img class="birthday-photo" src="birthday-night.webp" width="1024" height="1536" alt="은하수가 펼쳐진 밤하늘과 생크림 케이크" decoding="async">
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
    dialog.addEventListener('close', () => { stop(); if (opener?.isConnected) opener.focus(); });
    function toggleCandle() {
      const out = !dialog.classList.contains('candles-out');
      dialog.classList.toggle('candles-out', out);
      dialog.querySelector('.birthday-candle-button').setAttribute('aria-pressed', String(out));
      dialog.querySelector('.birthday-candle-button').setAttribute('aria-label', out ? '촛불 켜기' : '촛불 끄기');
      dialog.querySelector('.birthday-blow').textContent = out ? '촛불 켜기' : '촛불 끄기';
      if (out) celebrate();
    }
    dialog.querySelector('.birthday-candle-button').addEventListener('click', toggleCandle);
    dialog.querySelector('.birthday-blow').addEventListener('click', toggleCandle);
    dialog.querySelector('.birthday-replay').addEventListener('click', celebrate);
    dialog.querySelector('.birthday-primary').addEventListener('click', event => {
      stop();
      dialog.querySelector('.birthday-letter').hidden = false;
      event.currentTarget.setAttribute('aria-expanded', 'true');
      dialog.querySelector('.birthday-letter').focus({preventScroll:true});
      dialog.querySelector('.birthday-letter').scrollIntoView({behavior:motion.matches?'auto':'smooth', block:'start'});
    });
    document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
    motion.addEventListener('change', stop);
    window.addEventListener('resize', stop);
  }
  window.ScheduleBirthday = {
    open() {
      if (!dialog) create();
      if (dialog.open) return;
      opener = document.activeElement;
      dialog.classList.remove('candles-out');
      dialog.querySelector('.birthday-candle-button').setAttribute('aria-pressed', 'false');
      dialog.querySelector('.birthday-candle-button').setAttribute('aria-label', '촛불 끄기');
      dialog.querySelector('.birthday-blow').textContent = '촛불 끄기';
      dialog.querySelector('.birthday-letter').hidden = true;
      dialog.querySelector('.birthday-primary').setAttribute('aria-expanded', 'false');
      dialog.showModal();
      dialog.querySelector('.birthday-scroll').scrollTop = 0;
      celebrate();
    }
  };
})();
