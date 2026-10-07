// ============================================================================
// 独立登录页。加载在 login.html 上，和主界面（index.html / app.js）完全分开。
//
// 为什么单独一个页面：登录不是主界面上的一层蒙版。
// 之前用 body > fixed 浮层，登录成功后浮层删了、body 上的 login-locked
// 忘了摘，主界面就带着 blur(6px) 显示 —— 登录进去是个虚的。
// 独立页面没有"浮层盖住主界面"这个状态，登录成功直接 location.replace。
// ============================================================================
(function () {
  'use strict';

  const form = document.getElementById('login-form');
  const userInput = document.getElementById('login-user');
  const passInput = document.getElementById('login-pass');
  const error = document.getElementById('login-error');
  const submit = document.getElementById('login-submit');

  let busy = false;

  function setBusy(state) {
    busy = state;
    submit.disabled = state;
    submit.textContent = state ? '正在登录…' : '登录';
  }

  // X-Nexus-Demo 是应用自己的来源校验头，所有写操作都要带。
  // 少了它，后端会在中间件里直接 403 "请求来源校验失败" —— 和密码对错无关。
  function headers() {
    return { 'Content-Type': 'application/json', 'X-Nexus-Demo': '1' };
  }

  async function getJSON(path) {
    const response = await fetch(path, { headers: headers() });
    return { ok: response.ok, status: response.status, data: await response.json().catch(() => ({})) };
  }

  // 公网模式下如果已登录,不直接跳 / (会被 authGate 弹回),而是给个"继续"按钮
  // 显式带 ?via=login 走一次放行通道。这样:
  //   - 直接访问 / 永远不会直接进 app (cookie 留着也没用,会跳 /login)
  //   - 已登录用户点 /login 也能继续进 (不会卡在表单前又重输一遍口令)
  //   - 不形成 / ↔ /login 死循环 (?via=login 是单次放行标记)
  async function skipIfAlreadyIn() {
    try {
      const { data } = await getJSON('/api/auth/state');
      if (!data.required) location.replace('/');
      else if (data.authenticated) showContinue(data.name);
      else userInput.focus();
    } catch (problem) {
      error.textContent = '无法连接服务：' + (problem.message || '请稍后重试。');
    }
  }

  function showContinue(name) {
    form.hidden = true;
    error.hidden = true;
    const note = document.createElement('p');
    note.className = 'login-sub';
    note.textContent = name ? `当前会话：${name}。` : '当前会话有效。';
    const cont = document.createElement('button');
    cont.type = 'button';
    cont.className = 'login-submit';
    cont.textContent = '继续进入';
    // ?via=login 是单次放行:authGate 看到 ?via=login + 已认证就启动 app,
    // 然后立刻 history.replaceState 清掉这个参数,不留痕迹也不形成循环。
    cont.addEventListener('click', () => location.replace('/?via=login'));
    const logout = document.createElement('button');
    logout.type = 'button';
    logout.className = 'login-foot-link';
    logout.textContent = '换一个账号';
    logout.style.cssText = 'background:none;border:none;color:var(--muted);margin-left:12px;cursor:pointer;text-decoration:underline;font-size:11.5px';
    logout.addEventListener('click', async () => {
      try { await fetch('/api/auth/logout', { method: 'POST', headers: headers() }); }
      catch {}
      location.replace('/login');
    });
    note.append(logout);
    panel.append(note, cont);
    cont.focus();
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;

    const username = userInput.value.trim();
    const password = passInput.value;
    if (!username || !password) {
      error.textContent = '请填写账号和密码。';
      (username ? passInput : userInput).focus();
      return;
    }

    setBusy(true);
    error.textContent = '';
    // 清空字段里的值而不是只清输入框：失败一次之后，页面上不该还留着口令。
    passInput.value = '';
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ username, password }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        error.textContent = data.message || '登录未完成，请检查输入后重试。';
        // 失败后焦点回到账号框，而不是停在密码框：用户多半是账号打错了。
        userInput.focus();
        userInput.select();
        return;
      }
      // 换掉历史记录而不是压一层，登录页不会留在"后退"里。
      // 带 ?via=login:authGate 看到才会放行 app,否则立刻跳回 /login。
      location.replace('/?via=login');
    } catch (problem) {
      error.textContent = '无法连接服务，请确认 Nexus 已启动。';
    } finally {
      setBusy(false);
    }
  });

  skipIfAlreadyIn();
})();
