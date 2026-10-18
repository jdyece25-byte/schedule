/* A date-only gift: no schedule writes, credentials, audio or external assets. */
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
    const colors = ['#ffd594', '#ffadc8', '#c2b0ff', '#fff3dd', '#9edbd5'];
    const particles = [];
    for (let burst = 0; burst < 5; burst++) {
      const x = width * (.15 + Math.random() * .7), y = height * (.15 + Math.random() * .4);
      for (let i = 0; i < 45; i++) {
        const angle = Math.random() * Math.PI * 2, speed = 35 + Math.random() * 150;
        particles.push({x, y, vx:Math.cos(angle)*speed, vy:Math.sin(angle)*speed,
          delay:burst*.65, life:1.7+Math.random()*.9, color:colors[i%colors.length], size:2+Math.random()*2});
      }
    }
    const started = performance.now();
    function draw(now) {
      const elapsed = (now - started) / 1000;
      ctx.clearRect(0, 0, width, height);
      for (const p of particles) {
        const age = elapsed - p.delay;
        if (age < 0 || age > p.life) continue;
        ctx.globalAlpha = Math.max(0, 1-age/p.life);
        ctx.fillStyle = p.color;
        ctx.beginPath();ctx.arc(p.x+p.vx*age, p.y+p.vy*age+35*age*age, p.size, 0, Math.PI*2);ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (elapsed < 5.4 && dialog.open && !document.hidden) frame = requestAnimationFrame(draw);
      else stop();
    }
    frame = requestAnimationFrame(draw);
  }
  function create() {
    dialog = document.createElement('dialog');
    dialog.className = 'birthday-dialog';
    dialog.setAttribute('aria-labelledby', 'birthday-title');
    dialog.innerHTML = `<button class="birthday-close" type="button" aria-label="생일 축하 닫기" autofocus>×</button>
      <canvas class="birthday-confetti" aria-hidden="true"></canvas>
      <div class="birthday-scroll"><section class="birthday-hero">
        <p class="birthday-eyebrow">OCTOBER 20 · FOR YOU</p>
        <h1 id="birthday-title">태어나줘서 고마워,<br><em>생일 축하해.</em></h1>
        <p class="birthday-subtitle">멀리 있어도, 오늘의 가장 따뜻한 마음은<br>누나에게 닿았으면 좋겠어.</p>
        <div class="birthday-cake" role="img" aria-label="촛불이 켜진 분홍색 생일 케이크"><div class="birthday-plate"></div><div class="birthday-layer"></div><div class="birthday-icing"></div><div class="birthday-candle"><div class="birthday-flame"></div></div></div>
        <button class="birthday-primary" type="button" aria-expanded="false" aria-controls="birthday-letter">누나에게 쓴 편지 열어보기 ♡</button>
        <button class="birthday-replay" type="button">한 번 더 축하하기 ✨</button>
      </section>
      <article class="birthday-letter" id="birthday-letter" hidden aria-labelledby="birthday-letter-title">
        <div class="birthday-letter-date">10.20 · WITH ALL MY LOVE</div><h2 id="birthday-letter-title" tabindex="-1">사랑하는 누나에게</h2>
        <div class="birthday-letter-body"></div><p class="birthday-sign">많이많이 사랑해<br>— 남친이</p><div class="birthday-letter-heart" aria-hidden="true">♡</div>
      </article></div>`;
    for (const text of paragraphs) {
      const p = document.createElement('p'); p.textContent = text;
      dialog.querySelector('.birthday-letter-body').appendChild(p);
    }
    document.body.appendChild(dialog);
    dialog.querySelector('.birthday-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { stop(); if (opener?.isConnected) opener.focus(); });
    dialog.querySelector('.birthday-replay').addEventListener('click', celebrate);
    dialog.querySelector('.birthday-primary').addEventListener('click', event => {
      stop();
      dialog.querySelector('.birthday-letter').hidden = false;
      event.currentTarget.setAttribute('aria-expanded', 'true');
      const heading = dialog.querySelector('#birthday-letter-title');
      heading.focus({preventScroll:true});
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
      dialog.querySelector('.birthday-letter').hidden = true;
      dialog.querySelector('.birthday-primary').setAttribute('aria-expanded', 'false');
      dialog.showModal();
      dialog.querySelector('.birthday-scroll').scrollTop = 0;
      celebrate();
    }
  };
})();
