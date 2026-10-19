/* 轻量录像机 —— 前端 */
'use strict';

function $(s) { return document.querySelector(s); }
function $$(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
  return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

var toastTimer = null;
function toast(msg, bad) {
  var el = $('#toast');
  el.textContent = msg;
  el.style.background = bad ? '#C0553B' : '#2B2419';
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2600);
}

function api(url, opt) {
  opt = opt || {};
  opt.headers = opt.headers || {};
  opt.credentials = 'same-origin';
  if (opt.method && opt.method !== 'GET' && !opt.json) opt.json = {};
  if (opt.json) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(opt.json);
    delete opt.json;
  }
  return fetch(url, opt).then(function (r) {
    return r.json().then(function (data) {
      if (r.status === 401) {
        $('#authPanel').hidden = false;
        $('#authError').textContent = data.msg || '请输入访问口令';
        throw new Error(data.msg || '需要登录');
      }
      return data;
    });
  });
}

$('#authForm').onsubmit = function (event) {
  event.preventDefault();
  api('/api/auth', { method: 'POST', json: { token: $('#authToken').value } })
    .then(function (result) {
      if (!result.ok) { $('#authError').textContent = result.msg || '登录失败'; return; }
      $('#authToken').value = '';
      $('#authPanel').hidden = true;
      wallState.sig = '';
      loadInitial();
    }).catch(function () { });
};

/* ---------------- Tab ---------------- */
$$('header nav button').forEach(function (b) {
  b.onclick = function () {
    $$('header nav button').forEach(function (x) { x.classList.remove('on'); });
    b.classList.add('on');
    ['wall', 'cams', 'rec', 'set'].forEach(function (t) {
      $('#tab-' + t).hidden = (t !== b.dataset.tab);
    });
    if (b.dataset.tab === 'rec') loadRecordings();   // 进入回放即刷新列表
  };
});

/* ---------------- 视频墙 ---------------- */
var wallState = { sig: '', cells: {} };

function renderWall(cams) {
  var list = (cams || []).filter(function (c) { return c.enabled; });
  var cols = $('#wallCols').value || '2';
  var sig = list.map(function (c) { return c.id; }).join('|') + '#' + cols;
  var wall = $('#wall');

  if (sig !== wallState.sig) {
    wallState.sig = sig;
    wallState.cells = {};
    if (!list.length) {
      wall.className = 'wall c1';
      wall.innerHTML = '<div class="empty">还没有摄像头 —— 去「摄像头」页扫描或手动添加</div>';
      return;
    }
    wall.className = 'wall c' + cols;
    wall.innerHTML = list.map(function (c) {
      return '<div class="cell" data-id="' + esc(c.id) + '">' +
        '<img src="/live/' + esc(c.id) + '" alt="">' +
        '<div class="bar"><span class="nm">' + esc(c.name) + '</span>' +
        '<span class="st"></span></div></div>';
    }).join('');
    list.forEach(function (c) {
      wallState.cells[c.id] = {
        st: wall.querySelector('.cell[data-id="' + c.id + '"] .st')
      };
    });
  }

  // 状态徽标就地更新（不重建 DOM，绝不闪黑）
  list.forEach(function (c) {
    var box = wallState.cells[c.id];
    if (!box || !box.st) return;
    var s = c.state === 'online'
      ? '<span class="tag on">在线 ' + (c.fps || 0) + 'fps</span>'
      : (c.state === 'connecting' ? '<span class="tag warn">连接中</span>'
        : '<span class="tag off">离线</span>');
    if (c.person) s += ' <span class="tag human">有人</span>';
    if (c.rec) s += ' <span class="tag warn">录像中</span>';
    if (box.html !== s) { box.html = s; box.st.innerHTML = s; }
  });
}

/* ---------------- 摄像头表格 ---------------- */
var camRows = {};
function renderCams(cams) {
  var tb = $('#camList');
  if (!cams.length) {
    tb.innerHTML = '<tr><td colspan="5" style="color:var(--txt3)">还没有摄像头</td></tr>';
    camRows = {};
    return;
  }
  var sig = JSON.stringify(cams.map(function (c) { return [c.id, c.name, c.url, c.device_name]; }));
  if (sig !== (renderCams.sig || '')) {
    renderCams.sig = sig;
    camRows = {};
    tb.innerHTML = cams.map(function (c) {
      return '<tr data-id="' + esc(c.id) + '">' +
        '<td>' + esc(c.name) + '</td>' +
        '<td style="color:var(--txt3);font-size:12px">' + esc(c.source_type === 'local' ? ('电脑摄像头 · ' + (c.device_name || c.name)) : c.url) + '</td>' +
        '<td class="cst"></td>' +
        '<td><label class="switch"><input type="checkbox" class="cdet"' +
        (c.detect ? ' checked' : '') + '><span></span></label></td>' +
        '<td><button class="line ctog"></button> ' +
        '<button class="danger cdel">删除</button></td></tr>';
    }).join('');
    cams.forEach(function (c) {
      var tr = tb.querySelector('tr[data-id="' + c.id + '"]');
      camRows[c.id] = { st: tr.querySelector('.cst'),
                        tog: tr.querySelector('.ctog'),
                        det: tr.querySelector('.cdet'),
                        del: tr.querySelector('.cdel') };
      (function (id) {
        camRows[id].tog.onclick = function () {
          api('/api/cameras/' + id + '/toggle', { method: 'POST' })
            .then(function (r) { toast(r.ok ? '已切换' : (r.msg || '失败'), !r.ok); loadCams(); });
        };
        camRows[id].del.onclick = function () {
          if (!confirm('确定删除这路摄像头？')) return;
          api('/api/cameras/' + id, { method: 'DELETE' })
            .then(function () { toast('已删除'); loadCams(); });
        };
        camRows[id].det.onchange = function () {
          api('/api/cameras/' + id + '/detect',
            { method: 'POST', json: { on: this.checked } })
            .then(function () { toast('已保存'); });
        };
      })(c.id);
    });
  }
  cams.forEach(function (c) {
    var r = camRows[c.id];
    if (!r) return;
    var s = c.state === 'online'
      ? '<span class="tag on">在线</span>'
      : (c.state === 'connecting' ? '<span class="tag warn">连接中</span>'
        : '<span class="tag off">离线</span>');
    if (c.rec) s += ' <span class="tag warn">录像中</span>';
    if (!c.enabled) s = '<span class="tag off">已停用</span>';
    if (c.error && c.enabled) s += '<div class="hint">' + esc(c.error) + '</div>';
    if (c.annotation_warning && c.enabled) s += '<div class="hint">⚠ ' + esc(c.annotation_warning) + '</div>';
    if (r.html !== s) { r.html = s; r.st.innerHTML = s; }
    r.tog.textContent = c.enabled ? '停用' : '启用';
  });
}

function loadCams() {
  api('/api/settings').then(function (s) {
    renderCams(s.cameras || []);
  });
}

/* Device enumeration never opens a camera; adding is a separate user action. */
var localDevices = [];
$('#btnLocalRefresh').onclick = function () {
  var button = this;
  button.disabled = true;
  $('#btnLocalAdd').disabled = true;
  $('#localDevice').disabled = true;
  $('#localHint').textContent = '正在读取摄像头列表…';
  api('/api/local-cameras').then(function (result) {
    localDevices = result.ok ? (result.devices || []) : [];
    $('#localDevice').innerHTML = '<option value="">请选择摄像头</option>' + localDevices.map(function (device, index) {
      return '<option value="' + esc(device.device_id) + '"' + (device.added ? ' disabled' : '') + '>' +
        esc((index + 1) + '. ' + device.name + (device.added ? '（已添加）' : '')) + '</option>';
    }).join('');
    $('#localDevice').disabled = !localDevices.length;
    $('#localHint').textContent = result.msg || '选择并添加后才打开镜头。已添加设备可在下方启用或停用；不采集麦克风。';
  }).catch(function (error) {
    localDevices = [];
    $('#localHint').textContent = '读取失败：' + error.message;
  }).finally(function () { button.disabled = false; });
};
$('#localDevice').onchange = function () {
  var selected = localDevices.find(function (device) { return device.device_id === $('#localDevice').value; });
  $('#btnLocalAdd').disabled = !selected || selected.added;
  if (selected && !$('#localName').value.trim()) $('#localName').value = selected.name;
};
$('#btnLocalAdd').onclick = function () {
  var selected = localDevices.find(function (device) { return device.device_id === $('#localDevice').value; });
  if (!selected || selected.added) { toast('请刷新并选择未添加的摄像头', true); return; }
  $('#btnLocalAdd').disabled = true;
  api('/api/cameras', { method: 'POST', json: {
    source_type: 'local', device_id: selected.device_id,
    name: $('#localName').value.trim() || selected.name, enabled: true, detect: true
  } }).then(function (result) {
    toast(result.ok ? '已添加，正在打开摄像头' : result.msg || '添加失败', !result.ok);
    if (result.ok) {
      $('#localName').value = '';
      loadCams();
      $('#btnLocalRefresh').click();
    } else { $('#btnLocalAdd').disabled = false; }
  }).catch(function (error) {
    toast(error.message || '添加失败', true);
    $('#btnLocalAdd').disabled = false;
  });
};

/* ---------------- 扫描 ---------------- */
var scanTimer = null;
var scanResults = [];

function pollScan() {
  api('/api/scan/status').then(function (s) {
    var pct = s.total ? Math.round(s.done / s.total * 100) : 0;
    $('#scProg').style.width = pct + '%';
    $('#scHint').textContent = s.running
      ? ('扫描中 ' + s.done + '/' + s.total + '（' + pct + '%）　已发现 ' +
         (s.results || []).length + ' 台')
      : ('完成：共发现 ' + (s.results || []).length + ' 台，用时 ' +
         (s.elapsed || 0) + ' 秒' + (s.error ? '　' + s.error : ''));
    var log = (s.log || []).slice(-14);
    $('#scLog').innerHTML = log.length
      ? log.map(function (l) { return '<div>' + esc(l) + '</div>'; }).join('')
      : '<div class="hint">—</div>';
    if (!s.running) {
      clearInterval(scanTimer); scanTimer = null;
      $('#btnScan').disabled = false; $('#btnScanStop').disabled = true;
      scanResults = s.results || [];
      renderScan(scanResults);
    }
  }).catch(function () {
    clearInterval(scanTimer); scanTimer = null;
    $('#btnScan').disabled = false; $('#btnScanStop').disabled = true;
  });
}

function renderScan(list) {
  var box = $('#scResult');
  if (!list.length) {
    box.innerHTML = '<div class="hint">没扫到设备。确认网段、端口、账号密码，'
      + '或直接手动填 RTSP 地址添加。</div>';
    return;
  }
  box.innerHTML = list.map(function (r, i) {
    return '<div class="scan-item"><span>' + esc(r.ip) +
      '　<span style="color:var(--txt3);font-size:12px">' + esc(r.url) +
      '</span></span><button data-i="' + i + '">添加</button></div>';
  }).join('') +
    '<div style="margin-top:10px"><button id="btnAddAll">一键添加全部 (' +
    list.length + ')</button></div>';

  $$('#scResult button[data-i]').forEach(function (b) {
    b.onclick = function () {
      var r = scanResults[+b.dataset.i];
      api('/api/cameras', { method: 'POST',
        json: { name: '摄像头 ' + r.ip, scan_index: r.scan_index } })
        .then(function (x) { toast(x.ok ? '已添加' : (x.msg || '失败'), !x.ok); loadCams(); });
    };
  });
  var all = $('#btnAddAll');
  if (all) all.onclick = function () {
    api('/api/cameras/batch', { method: 'POST',
      json: { items: scanResults.map(function (r) {
        return { name: '摄像头 ' + r.ip, scan_index: r.scan_index, ip: r.ip }; }) } })
      .then(function (x) {
        toast(x.ok ? ('已添加 ' + x.added + ' 路') : (x.msg || '失败'), !x.ok);
        loadCams();
      });
  };
}

$('#btnScan').onclick = function () {
  $('#btnScan').disabled = true; $('#btnScanStop').disabled = false;
  $('#scProg').style.width = '0%';
  $('#scHint').textContent = '正在启动…';
  $('#scResult').innerHTML = '';
  api('/api/scan', { method: 'POST', json: {
    range: $('#scRange').value.trim(),
    port: parseInt($('#scPort').value, 10) || 554,
    users: $('#scUsers').value, pwds: $('#scPwds').value
  } }).then(function (r) {
    if (r && r.ok === false) {
      toast(r.msg || '无法启动扫描', true);
      $('#btnScan').disabled = false; $('#btnScanStop').disabled = true;
      return;
    }
    scanTimer = setInterval(pollScan, 1000);
    pollScan();
  }).catch(function (e) {
    toast('启动扫描失败：' + e, true);
    $('#btnScan').disabled = false; $('#btnScanStop').disabled = true;
  });
};

$('#btnScanStop').onclick = function () {
  api('/api/scan/stop', { method: 'POST' }).then(function () {
    $('#btnScan').disabled = false; $('#btnScanStop').disabled = true;
    if (scanTimer) { clearInterval(scanTimer); scanTimer = null; }
  });
};

$('#btnAdd').onclick = function () {
  var url = $('#cfUrl').value.trim();
  if (!url) { toast('请填 RTSP 地址', true); return; }
  api('/api/cameras', { method: 'POST', json: {
    name: $('#cfName').value.trim() || url, url: url } })
    .then(function (r) {
      toast(r.ok ? '已添加' : (r.msg || '失败'), !r.ok);
      if (r.ok) { $('#cfName').value = ''; $('#cfUrl').value = ''; loadCams(); }
    });
};

/* ---------------- 设置 ---------------- */
function fillSettings(s) {
  if (!s) return;
  $('#siteName').textContent = (s.site && s.site.name) || '轻量录像机';
  var st = s.stream || {}, dt = s.detect || {}, rd = s.record || {}, ml = s.mail || {},
      sc = s.scan || {};
  $('#stFps').value = st.fps; $('#stWidth').value = st.width;
  $('#stTransport').value = st.transport || 'tcp'; $('#stRetry').value = st.retry_sec;
  $('#dtEn').checked = !!dt.enabled; $('#dtMode').value = dt.mode || 'auto';
  $('#localDetectHint').textContent = dt.enabled
    ? '沿用现有检测设置与事件录像规则；请确认下方这路摄像头“识别人”开关已打开。'
    : '全局人形检测已关闭：仍可预览和抽帧，但不会触发事件录像。请到“设置”启用检测。';
  $('#dtInt').value = dt.interval; $('#dtConfirm').value = dt.confirm;
  $('#dtGone').value = dt.gone_sec;
  $('#rdTl').value = rd.timelapse_sec; $('#rdKeep').value = rd.keep_days;
  $('#rdMax').value = rd.max_event_min;
  $('#rdBoxes').checked = rd.target_boxes !== false;
  $('#rdBoxColor').value = rd.target_box_color || '#F51616';
  $('#mlEn').checked = !!ml.enabled; $('#mlTime').value = ml.time || '08:00';
  $('#mlHost').value = ml.smtp_host || ''; $('#mlPort').value = ml.smtp_port || 465;
  $('#mlUser').value = ml.user || ''; $('#mlPass').value = ml.pass || '';
  $('#mlPass').placeholder = ml.pass_configured ? '已配置，留空保持原值' : '填写邮箱授权码';
  $('#accessToken').placeholder = s.server && s.server.token_configured
    ? '已配置，留空保持原值' : '设置后访问页面需要登录';
  $('#mlTo').value = ml.to || '';
  if (sc.range && !$('#scRange').value) $('#scRange').value = sc.range;
  $('#scPwds').placeholder = sc.pwds_configured ? '已配置，留空使用已有密码' : '填写摄像头密码';
  var cb = s.clawbot || {};
  $('#cbEn').checked = cb.enabled !== false;
  $('#cbAuto').checked = cb.auto_start !== false;
  $('#cbMaxMb').value = cb.max_video_mb || 50;
  var preview = s.preview || {};
  $('#pvEn').checked = preview.enabled === true;
  $('#pvAddress').value = preview.address || '';
  $('#pvPort').value = preview.port || 8803;
  $('#rpEn').checked = (s.remote_preview || {}).enabled === true;
  refreshClawbot();
  refreshPreview();
  refreshRemotePreview();
}

$('#btnSave').onclick = function () {
  api('/api/settings', { method: 'POST', json: {
    server: { token: $('#accessToken').value },
    stream: { fps: +$('#stFps').value || 8, width: +$('#stWidth').value || 480,
              transport: $('#stTransport').value, retry_sec: +$('#stRetry').value || 5 },
    detect: { enabled: $('#dtEn').checked, mode: $('#dtMode').value,
              interval: +$('#dtInt').value || 0.5, confirm: +$('#dtConfirm').value || 2,
              gone_sec: +$('#dtGone').value || 8 },
    record: { timelapse_sec: +$('#rdTl').value || 720, keep_days: +$('#rdKeep').value || 7,
              max_event_min: +$('#rdMax').value || 30, target_boxes: $('#rdBoxes').checked,
              target_box_color: $('#rdBoxColor').value },
    mail: { enabled: $('#mlEn').checked, time: $('#mlTime').value.trim() || '08:00',
            smtp_host: $('#mlHost').value.trim(), smtp_port: +$('#mlPort').value || 465,
            user: $('#mlUser').value.trim(), pass: $('#mlPass').value,
            to: $('#mlTo').value.trim() },
    scan: { range: $('#scRange').value.trim(), pwds: $('#scPwds').value },
    clawbot: { enabled: $('#cbEn').checked, auto_start: $('#cbAuto').checked,
               max_video_mb: +$('#cbMaxMb').value || 50 },
    preview: { enabled: $('#pvEn').checked, address: $('#pvAddress').value.trim(),
               port: +$('#pvPort').value || 8803 },
    remote_preview: { enabled: $('#rpEn').checked }
  } }).then(function (r) {
    toast(r.ok ? '已保存并生效' : (r.msg || '失败'), !r.ok);
    if (r.ok) {
      $('#accessToken').value = ''; $('#mlPass').value = ''; $('#scPwds').value = '';
      api('/api/settings').then(fillSettings);
    }
  });
};

/* ---------------- 微信 ClawBot ---------------- */
var cbTimer = null;

function refreshRemotePreview() {
  api('/api/remote-preview').then(function (p) {
    var active = p.state === 'starting' || p.state === 'ready';
    $('#rpStart').disabled = !p.enabled || active;
    $('#rpStop').disabled = !active;
    if (!p.enabled) { $('#rpState').textContent = '远程预览未选择；微信使用局域网预览设置。'; return; }
    if (p.error) { $('#rpState').textContent = '远程预览未就绪：' + p.error; return; }
    if (p.state === 'ready' && p.running) {
      $('#rpState').textContent = '远程连接已就绪。请在微信重新发送“预览1号”，使用新回复的 HTTPS 链接；手机可切换到移动网络测试。';
    } else if (p.state === 'starting') {
      $('#rpState').textContent = '正在建立远程连接，请稍候…';
    } else if (!p.installed) {
      $('#rpState').textContent = '请先在电脑安装并登录官方 NetBird 客户端，再按下方步骤开启 Peer Expose。';
    } else {
      $('#rpState').textContent = '已选择远程链接，本次尚未开启连接。确认下方配置后，点击“同意转发并开启远程预览”。';
    }
  }).catch(function () { $('#rpState').textContent = '暂时无法读取远程预览状态。'; });
}

$('#rpStart').onclick = function () {
  $('#rpStart').disabled = true;
  api('/api/remote-preview/start', {method: 'POST', json: {confirm_relay: true}}).then(function (r) {
    if (!r.ok) toast(r.msg || '远程预览无法开启', true);
    refreshRemotePreview();
  }).catch(function () { toast('无法连接电脑，请重试。', true); refreshRemotePreview(); });
};
$('#rpStop').onclick = function () {
  $('#rpStop').disabled = true;
  api('/api/remote-preview/stop', {method: 'POST', json: {}}).then(function (r) {
    toast(r.ok ? '远程预览已停止，旧链接失效' : (r.msg || '停止失败'), !r.ok);
    refreshRemotePreview();
  }).catch(function () { toast('无法连接电脑，请重试。', true); refreshRemotePreview(); });
};

function refreshPreview() {
  api('/api/preview').then(function (p) {
    if (!p.enabled) { $('#pvState').textContent = '局域网预览未开启。'; return; }
    if (p.error) { $('#pvState').textContent = '预览未就绪：' + p.error; return; }
    if (p.running) {
      $('#pvState').textContent = '已开启：' + p.address + ':' + p.port + '。请在微信发送“预览1号”；若手机打不开，请确认同一 Wi-Fi，并检查专用网络的防火墙许可。';
    } else {
      $('#pvState').textContent = '预览服务尚未就绪，请检查本机 Wi-Fi 地址和端口后重新保存。';
    }
  }).catch(function () { $('#pvState').textContent = '暂时无法读取局域网预览状态。'; });
}

function refreshClawbot() {
  api('/api/clawbot').then(function (d) {
    if (!d || !d.ok) { $('#cbState').textContent = '插件未就绪'; return; }
    var txt = '';
    if (d.logged) {
      txt = d.receiving
        ? '✅ 已登录，正在接收微信消息（剩余约 ' + d.remain_h + ' 小时）'
        : '已登录，但未在接收（剩余约 ' + d.remain_h + ' 小时）';
    } else if (d.state === 'wait') {
      txt = '等待扫码中…';
    } else if (d.state === 'scaned') {
      txt = '已扫码，请在手机上确认…';
    } else if (d.state === 'expired') {
      txt = '⚠️ 登录已过期，请重新扫码';
    } else if (d.state === 'error') {
      txt = '⚠️ ' + (d.error || '出错');
    } else {
      txt = '未登录，点「扫码登录」开始';
    }
    if (d.logged && d.error) txt += '；⚠️ ' + d.error;
    $('#cbState').textContent = txt;
    if (d.qr_url) {
      $('#cbBox').style.display = '';
      $('#cbQr').src = '/api/clawbot/qr.png?t=' + Date.now();
      $('#cbLink').href = d.qr_url;
      $('#cbHint').textContent = d.logged ? '已登录，无需再扫。' : '扫码后手机上点确认。';
    } else {
      $('#cbBox').style.display = 'none';
    }
    var polling = (d.state === 'wait' || d.state === 'scaned');
    if (polling && !cbTimer) {
      cbTimer = setInterval(refreshClawbot, 2000);
    } else if (!polling && cbTimer) {
      clearInterval(cbTimer); cbTimer = null;
    }
  }).catch(function () { });
}

$('#cbLogin').onclick = function () {
  toast('正在获取二维码…');
  api('/api/clawbot/login', { method: 'POST', json: {} }).then(function (r) {
    if (!r.ok) { toast('取二维码失败：' + (r.error || '未知'), true); return; }
    refreshClawbot();
    toast('请用微信扫码');
  });
};

$('#cbLogout').onclick = function () {
  api('/api/clawbot/logout', { method: 'POST', json: {} })
    .then(function () { refreshClawbot(); toast('已退出登录'); });
};

$('#btnMailTest').onclick = function () {
  toast('正在发送…');
  api('/api/mail/test', { method: 'POST', json: {} }).then(function (r) {
    toast(r.ok ? ('发送成功：' + r.msg) : ('失败：' + r.msg), !r.ok);
  });
};

$('#btnQuit').onclick = function () {
  if (!confirm('确定退出程序？')) return;
  api('/api/system/quit', { method: 'POST' }).then(function () {
    document.body.innerHTML = '<div class="empty">已退出</div>';
  });
};

/* ---------------- 轮询 ---------------- */
var cbSettingsPollAt = 0;
function tick() {
  if (!document.hidden && !$('#tab-set').hidden && !cbTimer &&
      Date.now() - cbSettingsPollAt >= 5000) {
    cbSettingsPollAt = Date.now();
    refreshClawbot();
    refreshPreview();
    refreshRemotePreview();
  }
  api('/api/status').then(function (s) {
    $('#localDetectHint').textContent = s.detection_enabled
      ? '沿用现有检测设置与事件录像规则；请确认下方这路摄像头“识别人”开关已打开。'
      : '全局人形检测已关闭：仍可预览和抽帧，但不会触发事件录像。请到“设置”启用检测。';
    renderWall(s.cams || []);
    var list = s.cams || [];
    if (!$('#tab-cams').hidden) renderCams(list);
    $('#mMem').textContent = (s.mem_mb == null ? '内存 -' : '内存 ' + s.mem_mb + 'MB');
    $('#mVer').textContent = 'v' + (s.version || '');
  }).catch(function () { });
}

$('#wallCols').onchange = function () { wallState.sig = ''; tick(); };

function loadInitial() {
api('/api/net').then(function (n) {
  if (n && n.range && !$('#scRange').value) $('#scRange').value = n.range;
  if (n && n.subnets && n.subnets.length) {
    var txt = '自动识别网段：' + n.subnets.join('、') +
      (n.subnets.length > 1 ? '（摄像头若在别的网卡上，在上方手动改范围）' : '');
    $('#scSub').textContent = txt;
  }
}).catch(function () { });

api('/api/settings').then(function (s) { fillSettings(s); loadCams(); }).catch(function () { });
tick();
}
loadInitial();
setInterval(tick, 1000);

/* ---------------- 回放 ---------------- */
var pbItems = [];

function loadRecordings() {
  var cam = $('#pbCam').value;
  var type = $('#pbType').value;
  var q = [];
  if (cam) q.push('cam=' + encodeURIComponent(cam));
  if (type) q.push('type=' + encodeURIComponent(type));
  api('/api/recordings' + (q.length ? '?' + q.join('&') : ''))
    .then(function (r) {
      pbItems = (r.items || []);
      fillPbCams(r.cams || []);
      renderPbList();
    })
    .catch(function (e) { toast('读取录像失败：' + e, true); });
}

function fillPbCams(cams) {
  var sel = $('#pbCam');
  var cur = sel.value;
  sel.innerHTML = '<option value="">全部</option>' +
    cams.map(function (c) {
      return '<option value="' + esc(c.id) + '">' + esc(c.name) + '</option>';
    }).join('');
  if (cams.some(function (c) { return c.id === cur; })) sel.value = cur;
}

function renderPbList() {
  var box = $('#pbList');
  if (!pbItems.length) {
    box.innerHTML = '<div class="hint" style="padding:16px">没有已完成的录像。</div>';
    return;
  }
  var html = '';
  var lastDate = '';
  pbItems.forEach(function (it, i) {
    if (it.date !== lastDate) {
      lastDate = it.date;
      html += '<div class="grp">' + esc(it.date) + '</div>';
    }
    var t = it.type === 'timelapse'
      ? '<span class="tag tl">抽帧</span>' : '<span class="tag warn">事件</span>';
    var sz = (it.size / 1048576).toFixed(1) + ' MB';
    html += '<div class="it" data-i="' + i + '">' +
      '<div class="t">' + t + ' ' + esc(it.cam_name) + '</div>' +
      '<div class="s">' + esc(it.title) + ' · ' + sz + '</div></div>';
  });
  box.innerHTML = html;
  $$('#pbList .it').forEach(function (el) {
    el.onclick = function () { playRec(pbItems[+el.dataset.i], el); };
  });
}

function playRec(item, el) {
  $$('#pbList .it').forEach(function (x) { x.classList.remove('on'); });
  if (el) el.classList.add('on');
  var v = $('#pbVideo');
  v.pause();
  v.removeAttribute('src');          // 干净切换，避免残留上一帧
  v.src = item.url;
  v.load();
  var pr = v.play();                 // 单 video 元素 + 后端 Range 支持 → 切换秒开
  if (pr && pr.catch) pr.catch(function () { });
  $('#pbInfo').textContent = item.cam_name + ' · ' + item.title;
}

$('#pbRefresh').onclick = loadRecordings;
$('#pbCam').onchange = loadRecordings;
$('#pbType').onchange = loadRecordings;
