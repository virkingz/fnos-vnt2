/* vnt-nas 管理页前端逻辑 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  // 计算 API 基址。
  // 本页既可能被直连访问（/），也可能被飞牛统一网关内嵌在
  // /app/vnt-nas/ 子路径下。后者若继续用绝对路径 /api/xx，
  // 请求会打到飞牛根目录而不是本应用，因此需要带上路径前缀。
  function detectApiBase() {
    var p = location.pathname || '/';
    var m = p.match(/^(\/app\/[^\/]+)\/?/);
    if (m) return m[1];
    return '';
  }

  var API = detectApiBase();
  var pollTimer = null;

  function toast(msg, isErr) {
    var t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (isErr ? ' err' : '');
    t.hidden = false;
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.hidden = true; }, 3400);
  }

  function req(path, opts) {
    opts = opts || {};
    return fetch(API + path, {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: 'no-store'
    }).then(function (r) {
      return r.json().catch(function () {
        throw new Error('响应不是合法 JSON（HTTP ' + r.status + '）');
      });
    });
  }

  function setMsg(id, text, ok) {
    var el = $(id);
    if (!el) return;
    el.className = 'msg ' + (ok ? 'ok' : 'err');
    el.textContent = text || '';
  }

  /* ---------------- 标签页 ---------------- */
  function initTabs() {
    var tabs = document.querySelectorAll('.tab');
    Array.prototype.forEach.call(tabs, function (b) {
      b.addEventListener('click', function () {
        Array.prototype.forEach.call(tabs, function (x) { x.classList.remove('active'); });
        Array.prototype.forEach.call(document.querySelectorAll('.pane'), function (p) {
          p.classList.remove('active');
        });
        b.classList.add('active');
        $('pane-' + b.dataset.tab).classList.add('active');
        if (b.dataset.tab === 'logs') loadLog();
        if (b.dataset.tab === 'panel' || b.dataset.tab === 'access') loadPanelInfo();
        if (b.dataset.tab === 'access') loadInstances();
      });
    });
  }

  /* ---------------- 状态 ---------------- */
  function renderState(d) {
    $('vInstalled').textContent = d.installed ? ('v' + d.installed) : '未下载';
    $('vArch').textContent = d.arch || '—';
    $('vAsset').textContent = d.asset || '当前架构无预编译包';
    $('vProc').textContent = d.core_running
      ? ('运行中 (PID ' + d.core_pid + ')') : '未运行';
    $('vPort').textContent = d.panel_port || '—';
    $('brandSub').textContent = '核心 ' + (d.installed ? 'v' + d.installed : '未下载')
      + ' · ' + (d.core_running ? '运行中' : '已停止');

    var pill = $('corePill');
    pill.textContent = d.core_running ? '核心运行中' : '核心已停止';
    pill.className = 'pill ' + (d.core_running ? 'on' : 'off');

    // 未下载核心时，把主按钮明确为「下载核心」
    if (!d.installed && !d.has_core) {
      var btn = $('btnUpdate');
      if (btn && !btn.dataset.ver) {
        btn.textContent = '下载核心';
        btn.disabled = false;
      }
    }
    if ($('tokenHint')) $('tokenHint').textContent = d.token_hint || '—';
    if ($('f_port') && document.activeElement !== $('f_port')) {
      $('f_port').value = d.panel_port || '';
    }
    if ($('mirror') && document.activeElement !== $('mirror')) {
      $('mirror').value = d.mirror || '';
    }
    if ($('proxy') && document.activeElement !== $('proxy')) {
      $('proxy').value = d.proxy || '';
    }
    if ($('f_bind') && document.activeElement !== $('f_bind')) {
      $('f_bind').value = d.bind || '0.0.0.0';
    }
    if ($('directUrl')) {
      $('directUrl').textContent = (d.bind === '0.0.0.0')
        ? ('组网面板地址：http://NAS地址:' + d.web_port)
        : '当前仅本机监听，组网面板只能在飞牛本机打开';
    }
    if ($('netPreview')) {
      var parts = [];
      parts.push(d.mirror ? ('镜像 ' + d.mirror) : '镜像 未设置');
      parts.push(d.proxy ? ('代理 ' + d.proxy) : '代理 未设置（直连）');
      if (d.proxy && d.proxy_is_socks && d.has_curl === false) {
        parts.push('⚠ SOCKS 需要 curl');
      }
      $('netPreview').textContent = parts.join('　|　');
    }

    if (d.is_admin === false) {
      Array.prototype.forEach.call(
        document.querySelectorAll('.btn.primary, #btnStartCore, #btnStopCore'),
        function (b) { b.disabled = true; });
    }
    return d;
  }

  function refresh() {
    return req('/api/state').then(function (r) {
      if (r.code !== 0) throw new Error(r.msg);
      return renderState(r.data);
    }).catch(function (e) { toast('读取状态失败：' + e.message, true); });
  }

  /* ---------------- 手动上传核心 ---------------- */
  function uploadCore() {
    var input = $('f_corefile');
    if (!input || !input.files || !input.files.length) {
      setMsg('uploadMsg', '请先选择核心文件（zip 或 vnt2_web）', false);
      return;
    }
    var file = input.files[0];
    var ver = ($('f_corever').value || '').trim();

    var fd = new FormData();
    fd.append('core', file, file.name);
    if (ver) fd.append('version', ver);

    var b = $('btnUpload');
    b.disabled = true;
    b.textContent = '上传中 0%';
    setMsg('uploadMsg', '正在上传 ' + file.name
      + '（' + (file.size / 1048576).toFixed(1) + ' MB）…', true);

    // 用 XHR 以便显示上传进度
    var xhr = new XMLHttpRequest();
    xhr.open('POST', API + '/api/upload_core', true);
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var pct = Math.round(e.loaded * 100 / e.total);
        b.textContent = '上传中 ' + pct + '%';
        $('progMsg').textContent = '上传中 ' + pct + '%';
      }
    };
    xhr.onload = function () {
      b.disabled = false;
      b.textContent = '上传并安装';
      var r = {};
      try { r = JSON.parse(xhr.responseText); } catch (e) { r = {}; }
      if (xhr.status === 200 && r.code === 0) {
        setMsg('uploadMsg', r.msg || '已开始安装', true);
        toast(r.msg || '已开始安装');
        $('progressCard').hidden = false;
        $('updLog').textContent = '';
        pollProgress();
        input.value = '';
      } else {
        setMsg('uploadMsg', r.msg || ('上传失败 HTTP ' + xhr.status), false);
        toast(r.msg || '上传失败', true);
      }
    };
    xhr.onerror = function () {
      b.disabled = false;
      b.textContent = '上传并安装';
      setMsg('uploadMsg', '上传中断，请重试', false);
    };
    xhr.send(fd);
  }

  /* ---------------- 检查 / 下载核心 ---------------- */
  function applyCheckResult(d) {
    var btn = $('btnUpdate');
    var tip = $('coreTip');
    if (d.action === 'install') {
      btn.textContent = '下载核心';
      btn.disabled = false;
      btn.dataset.ver = d.latest || '';
    } else if (d.action === 'update') {
      btn.textContent = '更新到 v' + d.latest;
      btn.disabled = false;
      btn.dataset.ver = d.latest || '';
    } else {
      btn.textContent = '已是最新';
      btn.disabled = true;
      btn.dataset.ver = '';
    }
    if (tip) {
      tip.textContent = d.tip || '';
      tip.className = 'hint' + (d.action === 'none' ? ' ok' : '');
    }
  }

  function checkUpdate() {
    var b = $('btnCheck');
    b.disabled = true; b.textContent = '检查中…';
    req('/api/check_update').then(function (r) {
      var d = r.data || {};
      if (r.code !== 0) {
        $('vLatest').textContent = '检查失败';
        if ($('coreTip')) {
          $('coreTip').textContent = (r.msg || '检查失败')
            + '　可到「访问设置」填镜像前缀重试，或用下方「手动上传核心」。';
          $('coreTip').className = 'hint err';
        }
        // 查询失败时仍允许尝试下载（也许只是 API 不通）
        if (!d.has_core) {
          var btn = $('btnUpdate');
          btn.textContent = '下载核心';
          btn.disabled = false;
        }
        toast(r.msg, true);
        return;
      }
      $('vLatest').textContent = 'v' + d.latest
        + (d.action === 'none' ? ' （已是最新）' : '');
      if (d.has_core && !d.probe_ok && d.probe_info) {
        $('coreTip').textContent = '当前核心无法运行：' + d.probe_info;
        $('coreTip').className = 'hint err';
      }
      applyCheckResult(d);
      toast(d.tip || '检查完成');
    }).catch(function (e) {
      toast('检查失败：' + e.message, true);
    }).then(function () {
      b.disabled = false; b.textContent = '检查更新';
    });
  }

  function startUpdate() {
    var ver = $('btnUpdate').dataset.ver || '';
    var what = ver ? ('v' + ver) : '最新核心';
    if (!confirm('将下载 ' + what + ' 并替换当前 vnt2_web，期间组网会短暂中断。继续？')) return;
    var b = $('btnUpdate');
    b.disabled = true;
    req('/api/update', { method: 'POST', body: ver ? { version: ver } : {} })
      .then(function (r) {
        if (r.code !== 0) throw new Error(r.msg);
        $('progressCard').hidden = false;
        $('updLog').textContent = '';
        toast('下载任务已启动');
        pollProgress();
      }).catch(function (e) {
        b.disabled = false;
        toast(e.message, true);
      });
  }

  function pollProgress() {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      req('/api/update/progress').then(function (r) {
        if (r.code !== 0) return;
        var u = r.data.update;
        $('barIn').style.width = (u.progress || 0) + '%';
        $('progPct').textContent = (u.progress || 0) + '%';
        $('progMsg').textContent = u.error || u.message || u.stage;
        if (!u.running) {
          clearInterval(pollTimer);
          $('btnUpdate').disabled = false;
          if (u.stage === 'done') {
            toast('核心已就绪：v' + u.target);
            checkUpdate();   // 刷新按钮状态为「已是最新」
          } else if (u.stage === 'error') {
            toast(u.error || '下载失败', true);
          }
          refresh();
        }
      });
    }, 800);
  }

  /* ---------------- 核心启停 ---------------- */
  function coreAction(action) {
    req('/api/core/' + action, { method: 'POST' }).then(function (r) {
      toast(r.msg, r.code !== 0);
      refresh();
      // 核心状态变了，面板可用性提示要跟着更新
      setTimeout(loadPanelInfo, 800);
    }).catch(function (e) { toast(e.message, true); });
  }

  /* ---------------- 访问设置 ---------------- */
  function saveToken() {
    var raw = $('f_token').value.trim();
    if (raw && raw.length < 16) {
      setMsg('tokenMsg', '令牌至少需要 16 位字符', false);
      return;
    }
    var b = $('btnSaveToken');
    b.disabled = true; b.textContent = '保存中…';
    req('/api/token', { method: 'POST', body: { token: raw } }).then(function (r) {
      setMsg('tokenMsg', r.msg, r.code === 0);
      toast(r.msg, r.code !== 0);
      $('f_token').value = '';
      refresh();
    }).catch(function (e) {
      setMsg('tokenMsg', e.message, false);
    }).then(function () {
      b.disabled = false; b.textContent = '保存令牌';
    });
  }

  function genToken() {
    var chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    var out = '';
    var buf = new Uint8Array(32);
    (window.crypto || window.msCrypto).getRandomValues(buf);
    for (var i = 0; i < 32; i++) out += chars[buf[i] % chars.length];
    $('f_token').value = out;
    setMsg('tokenMsg', '已生成随机令牌，点「保存令牌」生效', true);
  }

  function savePort() {
    var v = ($('f_port').value || '').trim();
    if (!/^[0-9]+$/.test(v) || +v < 1 || +v > 65535) {
      setMsg('portMsg', '端口必须是 1-65535 的数字', false);
      return;
    }
    var b = $('btnSavePort');
    b.disabled = true; b.textContent = '保存中…';
    req('/api/panel_port', { method: 'POST', body: { port: v } })
      .then(function (r) {
        setMsg('portMsg', r.msg, r.code === 0);
        toast(r.msg, r.code !== 0);
      }).catch(function (e) {
        setMsg('portMsg', e.message, false);
      }).then(function () {
        b.disabled = false; b.textContent = '保存并重启';
      });
  }

  /* ---------------- 组网面板监听范围 ---------------- */
  function saveBind() {
    var v = $('f_bind').value;
    var b = $('btnSaveBind');
    b.disabled = true; b.textContent = '保存中…';
    req('/api/bind', { method: 'POST', body: { bind: v } })
      .then(function (r) {
        setMsg('bindMsg', r.msg, r.code === 0);
        toast(r.msg, r.code !== 0);
        refresh();
        loadPanelInfo();
      })
      .catch(function (e) {
        setMsg('bindMsg', e.message, false);
      })
      .then(function () {
        b.disabled = false; b.textContent = '保存并重启核心';
      });
  }

  /* ---------------- 下载源与代理 ---------------- */
  function saveNet() {
    var m = $('mirror').value;
    var p = $('proxy').value;
    var b = $('btnSaveNet');
    b.disabled = true; b.textContent = '保存中…';
    // 镜像与代理是两个独立接口，依次保存
    req('/api/mirror', { method: 'POST', body: { mirror: m } })
      .then(function (r1) {
        if (r1.code !== 0) throw new Error(r1.msg);
        return req('/api/proxy', { method: 'POST', body: { proxy: p } });
      })
      .then(function (r2) {
        setMsg('netMsg', r2.msg, r2.code === 0);
        toast(r2.msg, r2.code !== 0);
        refresh();
      })
      .catch(function (e) {
        setMsg('netMsg', e.message, false);
        toast(e.message, true);
      })
      .then(function () {
        b.disabled = false; b.textContent = '保存';
      });
  }

  function testNet() {
    var p = ($('proxy').value || '').trim();
    var b = $('btnTestNet');
    b.disabled = true; b.textContent = '测试中…';
    setMsg('netMsg', '正在连接 GitHub，请稍候…', true);
    req('/api/proxy_test', { method: 'POST', body: { proxy: p } })
      .then(function (r) {
        setMsg('netMsg', r.msg, r.code === 0);
        toast(r.msg, r.code !== 0);
        if (r.code === 0) refresh();
      })
      .catch(function (e) {
        setMsg('netMsg', '测试失败：' + e.message, false);
      })
      .then(function () {
        b.disabled = false; b.textContent = '测试连通性';
      });
  }

  function loadInstances() {
    req('/api/instances').then(function (r) {
      if (r.code !== 0) throw new Error(r.msg);
      var list = r.data.instances || [];
      var el = $('instList');
      if (!list.length) {
        el.innerHTML = '<span class="hint">还没有配置任何组网，去「组网面板」里添加吧。</span>';
        return;
      }
      var html = '<table class="tbl"><thead><tr><th>配置文件</th>'
        + '<th>大小</th><th>修改时间</th></tr></thead><tbody>';
      list.forEach(function (x) {
        html += '<tr><td class="mono">' + esc(x.file_name) + '</td><td>'
          + Math.round(x.size / 1024 * 10) / 10 + ' KB</td><td>' + esc(x.mtime)
          + '</td></tr>';
      });
      el.innerHTML = html + '</tbody></table>';
    }).catch(function (e) {
      $('instList').innerHTML = '<span class="hint">' + esc(e.message) + '</span>';
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ---------------- 组网面板 ---------------- */
  function loadPanelInfo() {
    req('/api/panel').then(function (r) {
      if (r.code !== 0) throw new Error(r.msg);
      var d = r.data;
      $('panelUrl').textContent = d.display_url || d.url;
      $('panelReady').textContent = !d.has_token
        ? '尚未设置访问令牌，请先到「访问设置」设置'
        : (d.core_running ? '核心运行中，已内嵌显示（代理已注入令牌）'
                          : '核心未运行，请先启动核心');

      // 内嵌：面板经本应用 /core/ 反代，同源、无需令牌
      var f = $('coreFrame');
      if (f && d.core_running && d.has_token && d.url) {
        if (f.getAttribute('src') !== d.url) f.src = d.url;
      } else if (f) {
        f.src = 'about:blank';
      }
    }).catch(function (e) {
      $('panelUrl').textContent = e.message;
      $('panelReady').textContent = '—';
    });
  }

  function openPanel() {
    req('/api/panel').then(function (r) {
      if (r.code !== 0) throw new Error(r.msg);
      var d = r.data;
      if (!d.has_token) {
        toast('请先在「访问设置」中设置访问令牌', true);
        return;
      }
      if (!d.core_running) {
        toast('核心未运行，请先启动核心', true);
        return;
      }
      // 同源路径，新窗口打开即为飞牛地址下的面板
      window.open(d.url, '_blank');
      toast('已在新窗口打开组网面板');
    }).catch(function (e) { toast(e.message, true); });
  }

  function reloadFrame() {
    var f = $('coreFrame');
    if (f) {
      var src = f.getAttribute('src');
      f.src = 'about:blank';
      setTimeout(function () { f.src = src; }, 60);
    }
    loadPanelInfo();
    toast('已重新加载内嵌面板');
  }

  // 内嵌加载后若内容为空，给出提示，避免静默白屏
  function watchFrame() {
    var f = $('coreFrame');
    var tip = $('frameTip');
    if (!f) return;
    f.addEventListener('load', function () {
      // 同源才能读 contentDocument；本代理是同源的，正常可读
      var doc = null;
      try { doc = f.contentDocument; } catch (e) { doc = null; }
      if (!doc || !tip) return;
      var txt = (doc.body && doc.body.innerText)
        ? doc.body.innerText.trim() : '';
      var hasNode = doc.querySelector('div,table,form,section,main');
      if (!doc.body || (!txt && !hasNode)) {
        tip.hidden = false;
        tip.textContent = '面板已加载但内容为空。请点「自检面板可用性」查看原因，'
          + '或先确认核心已下载并启动。';
      } else {
        tip.hidden = true;
      }
    });
  }

  function copyPanelUrl() {
    req('/api/panel').then(function (r) {
      if (r.code !== 0) throw new Error(r.msg);
      var u = r.data.url;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(u).then(function () {
          toast('已复制：' + u);
        }).catch(function () { toast('面板地址：' + u); });
      } else {
        toast('面板地址：' + u);
      }
    }).catch(function (e) { toast(e.message, true); });
  }

  /* ---------------- 面板自检 ---------------- */
  function runDiag() {
    var b = $('btnDiag');
    var out = $('diagText');
    b.disabled = true; b.textContent = '自检中…';
    req('/api/diag').then(function (r) {
      var d = r.data || {};
      var lines = ['面板地址：' + (d.panel_url || '—'),
                   '请求 Host：' + (d.host || '—'),
                   '挂载前缀：' + (d.mount || '/'),
                   ''];
      (d.items || []).forEach(function (it) {
        lines.push((it.ok ? '[正常] ' : '[问题] ') + it.name + ' —— ' + it.detail);
      });
      var bad = (d.items || []).filter(function (x) { return !x.ok; });
      if (bad.length) {
        lines.push('', '建议：先处理上面标为「问题」的项。');
      }
      out.hidden = false;
      out.textContent = lines.join('\n');
      toast(bad.length ? ('发现 ' + bad.length + ' 个问题') : '自检通过', !!bad.length);
    }).catch(function (e) {
      out.hidden = false;
      out.textContent = '自检失败：' + e.message;
    }).then(function () {
      b.disabled = false; b.textContent = '自检面板可用性';
    });
  }

  /* ---------------- 日志 ---------------- */
  function loadLog() {
    var name = $('logName').value;
    req('/api/logs?name=' + name).then(function (r) {
      $('logText').textContent = (r.code === 0 && r.data.text)
        ? r.data.text : '（暂无日志）';
    }).catch(function (e) { $('logText').textContent = e.message; });
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    $('btnRefresh').addEventListener('click', function () { refresh(); toast('已刷新'); });
    $('btnCheck').addEventListener('click', checkUpdate);
    $('btnUpdate').addEventListener('click', startUpdate);
    $('btnStartCore').addEventListener('click', function () { coreAction('start'); });
    $('btnStopCore').addEventListener('click', function () { coreAction('stop'); });
    $('btnStartCore2').addEventListener('click', function () { coreAction('start'); });
    $('btnSaveToken').addEventListener('click', saveToken);
    $('btnGenToken').addEventListener('click', genToken);
    $('btnSavePort').addEventListener('click', savePort);
    $('btnOpenPanel').addEventListener('click', openPanel);
    $('btnUpload').addEventListener('click', uploadCore);
    $('btnLog').addEventListener('click', loadLog);
    $('logName').addEventListener('change', loadLog);
    $('btnSaveNet').addEventListener('click', saveNet);
    $('btnTestNet').addEventListener('click', testNet);
    $('btnSaveBind').addEventListener('click', saveBind);
    $('btnDiag').addEventListener('click', runDiag);
    $('btnCopyPanel').addEventListener('click', copyPanelUrl);
    $('btnReloadFrame').addEventListener('click', reloadFrame);
  }

  function boot() {
    initTabs();
    bind();
    watchFrame();
    refresh();
    loadPanelInfo();
    loadInstances();
    // 支持 #panel / #access / #logs 直接定位到某个标签
    // （桌面上的「VNT 组网面板」入口即用 #panel 直达）
    var hash = (location.hash || '').replace('#', '');
    if (hash) {
      var t = document.querySelector('.tab[data-tab="' + hash + '"]');
      if (t) t.click();
    }
    // 若后台已有更新任务，自动接管进度显示
    req('/api/update/progress').then(function (r) {
      if (r.code === 0 && r.data.update && r.data.update.running) {
        $('progressCard').hidden = false;
        pollProgress();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
