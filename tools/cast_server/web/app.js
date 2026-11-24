/* 程序说明：Waveshare ESP32-S3-RLCD-4.2 无线投屏网页端控台前端脚本（无外部构建依赖） */

(() => {
  'use strict';

  const form = document.getElementById('config-form');
  const statusBanner = document.getElementById('global-status');
  const previewImg = document.getElementById('preview-img');
  const metaSha = document.getElementById('meta-sha');
  const metaPushInfo = document.getElementById('meta-push-info');
  const serviceBadge = document.getElementById('service-badge');
  const togglePauseBtn = document.getElementById('btn-toggle-pause');
  const thresholdRange = document.getElementById('THRESHOLD');
  const thresholdVal = document.getElementById('threshold-val');
  const fileInput = document.getElementById('image-file-input');
  const dropZone = document.getElementById('drop-zone');
  const curlBox = document.getElementById('curl-example');
  const toggleTokenBtn = document.getElementById('toggle-token-btn');
  const tokenInput = document.getElementById('CAST_TOKEN');

  let currentPaused = false;
  let healthPollTimer = null;
  let devicePollTimer = null;
  let healthInFlight = false;
  let deviceInFlight = false;
  let lastPreviewKey = '';

  /* 全页单一 polite live region，按交互后验证规范延迟播报校验错误 */
  const announcer = document.createElement('div');
  announcer.setAttribute('aria-live', 'polite');
  announcer.classList.add('visually-hidden');
  document.body.append(announcer);
  let announceTimer = null;
  let announcedField = null;

  const linkError = (el, shown) => {
    const errorId = el.dataset.error;
    if (!errorId) return;
    const ids = (el.getAttribute('aria-describedby') || '')
      .split(/\s+/)
      .filter((id) => id && id !== errorId);
    if (shown) ids.push(errorId);
    if (ids.length) el.setAttribute('aria-describedby', ids.join(' '));
    else el.removeAttribute('aria-describedby');
  };

  const spokenText = (node) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove());
    return copy.textContent.replace(/\s+/g, ' ').trim();
  };

  const announceError = (el) => {
    const errNode = document.getElementById(el.dataset.error);
    if (!errNode) return;
    const labelText = el.labels?.[0] ? `${spokenText(el.labels[0])}: ` : '';
    clearTimeout(announceTimer);
    announcer.textContent = '';
    announcedField = el;
    announceTimer = setTimeout(() => {
      announcer.textContent = labelText + spokenText(errNode);
    }, 500);
  };

  /* 自定义范围校验确保文本数字框输入精确落在后端规则内 */
  const customRangeValidators = {
    DEVICE_PORT: (v) => {
      const n = Number(v);
      return Number.isInteger(n) && n >= 1 && n <= 65535 ? '' : '端口需为 1–65535 整数';
    },
    HTTP_TIMEOUT_SEC: (v) => {
      const n = Number(v);
      return Number.isFinite(n) && n >= 0.2 && n <= 60 ? '' : '超时需为 0.2–60 秒数字';
    },
    PUSH_INTERVAL_MS: (v) => {
      const n = Number(v);
      return Number.isInteger(n) && n >= 50 && n <= 3600000 ? '' : '间隔需为 50–3600000 整数';
    },
    CAST_TIMEOUT_SEC: (v) => {
      const n = Number(v);
      return Number.isInteger(n) && n >= 0 && n <= 86400 ? '' : '超时回退需为 0–86400 整数';
    },
    HEARTBEAT_INTERVAL_SEC: (v) => {
      const n = Number(v);
      return Number.isInteger(n) && n >= 0 && n <= 3600 ? '' : '心跳间隔需为 0–3600 整数';
    },
  };

  const applyCustomConstraint = (el) => {
    const checker = customRangeValidators[el.name];
    if (!checker) return;
    el.setCustomValidity('');
    if (el.validity.valid) {
      const msg = checker(el.value.trim());
      if (msg) el.setCustomValidity(msg);
    }
  };

  const flagged = new WeakSet();
  const announced = new WeakSet();
  const dirtyMap = new WeakMap();
  const supportsUserInvalid = CSS.supports('selector(:user-invalid)');

  const syncFieldAria = (el, announce = false) => {
    if (!el || !el.matches?.('input')) return;
    applyCustomConstraint(el);
    const invalid = supportsUserInvalid
      ? el.matches(':user-invalid')
      : el.classList.contains('user-invalid-fallback');

    if (invalid) {
      if (announce && !announced.has(el)) {
        announceError(el);
        announced.add(el);
      }
      el.setAttribute('aria-invalid', 'true');
      linkError(el, true);
      flagged.add(el);
    } else if (flagged.has(el) || el.getAttribute('aria-invalid') !== 'true') {
      el.setAttribute('aria-invalid', 'false');
      linkError(el, false);
      flagged.delete(el);
      announced.delete(el);
      if (announcedField === el) {
        clearTimeout(announceTimer);
        announcer.textContent = '';
      }
    }
  };

  const initFieldValidation = () => {
    form.querySelectorAll('input').forEach((el) => {
      if (el.type === 'text' || el.type === 'password') {
        el.setAttribute('aria-invalid', 'false');
      }
    });

    form.addEventListener('blur', (e) => {
      const el = e.target;
      if (!supportsUserInvalid && el.checkValidity) {
        applyCustomConstraint(el);
        const st = dirtyMap.get(el) || { interacted: false, blurred: false };
        st.blurred = true;
        dirtyMap.set(el, st);
        if (st.interacted) {
          el.classList.toggle('user-invalid-fallback', !el.validity.valid);
          el.classList.toggle('user-valid-fallback', el.validity.valid);
        }
      }
      syncFieldAria(el, true);
    }, true);

    form.addEventListener('input', (e) => {
      const el = e.target;
      applyCustomConstraint(el);
      if (!supportsUserInvalid && el.checkValidity) {
        const st = dirtyMap.get(el) || { interacted: false, blurred: false };
        st.interacted = true;
        dirtyMap.set(el, st);
        if (st.blurred) {
          el.classList.toggle('user-invalid-fallback', !el.validity.valid);
          el.classList.toggle('user-valid-fallback', el.validity.valid);
        }
      }
      if (el.hasAttribute('aria-invalid')) syncFieldAria(el, false);
    });

    form.addEventListener('invalid', (e) => {
      const el = e.target;
      if (!supportsUserInvalid) {
        el.classList.add('user-invalid-fallback');
        el.classList.remove('user-valid-fallback');
      }
      announced.add(el);
      syncFieldAria(el, false);
    }, true);
  };

  const showStatus = (message, isError = false) => {
    statusBanner.textContent = message;
    statusBanner.classList.add('visible');
    statusBanner.classList.toggle('error', isError);
  };

  const refreshPreviewImage = (key = '') => {
    const ver = key ? `v=${encodeURIComponent(key)}&` : '';
    previewImg.src = `/preview.png?${ver}ts=${Date.now()}`;
  };

  const fillConfigForm = (cfg) => {
    document.getElementById('DEVICE_IP').value = cfg.DEVICE_IP ?? '';
    document.getElementById('DEVICE_PORT').value = cfg.DEVICE_PORT ?? 8080;
    document.getElementById('HTTP_TIMEOUT_SEC').value = cfg.HTTP_TIMEOUT_SEC ?? 3.0;
    document.getElementById('CAST_TOKEN').value = cfg.CAST_TOKEN ?? '';
    document.getElementById('PUSH_INTERVAL_MS').value = cfg.PUSH_INTERVAL_MS ?? 1000;
    document.getElementById('CAST_TIMEOUT_SEC').value = cfg.CAST_TIMEOUT_SEC ?? 30;
    document.getElementById('HEARTBEAT_INTERVAL_SEC').value = cfg.HEARTBEAT_INTERVAL_SEC ?? 10;
    document.getElementById('SKIP_UNCHANGED').checked = Boolean(cfg.SKIP_UNCHANGED);
    document.getElementById('AUTO_SWITCH').checked = Boolean(cfg.AUTO_SWITCH);
    document.getElementById('INVERT').checked = Boolean(cfg.INVERT);

    thresholdRange.value = String(cfg.THRESHOLD ?? 128);
    thresholdVal.textContent = thresholdRange.value;

    const ditherRadio = form.querySelector(`input[name="DITHER_MODE"][value="${cfg.DITHER_MODE}"]`);
    if (ditherRadio) ditherRadio.checked = true;
    const fitRadio = form.querySelector(`input[name="FIT_MODE"][value="${cfg.FIT_MODE}"]`);
    if (fitRadio) fitRadio.checked = true;
    const rotRadio = form.querySelector(`input[name="ROTATE"][value="${cfg.ROTATE}"]`);
    if (rotRadio) rotRadio.checked = true;

    const origin = window.location.origin;
    curlBox.textContent =
      `curl -X POST "${origin}/push?push_now=1" -H "Content-Type: image/png" --data-binary @frame.png\n` +
      `curl -X POST "${origin}/push" -F "image=@frame.png"`;
  };

  const collectConfigPayload = () => {
    const fd = new FormData(form);
    return {
      DEVICE_IP: String(fd.get('DEVICE_IP') || '').trim(),
      DEVICE_PORT: Number(fd.get('DEVICE_PORT')),
      HTTP_TIMEOUT_SEC: Number(fd.get('HTTP_TIMEOUT_SEC')),
      CAST_TOKEN: String(fd.get('CAST_TOKEN') || ''),
      PUSH_INTERVAL_MS: Number(fd.get('PUSH_INTERVAL_MS')),
      CAST_TIMEOUT_SEC: Number(fd.get('CAST_TIMEOUT_SEC')),
      HEARTBEAT_INTERVAL_SEC: Number(fd.get('HEARTBEAT_INTERVAL_SEC')),
      SKIP_UNCHANGED: document.getElementById('SKIP_UNCHANGED').checked,
      AUTO_SWITCH: document.getElementById('AUTO_SWITCH').checked,
      INVERT: document.getElementById('INVERT').checked,
      DITHER_MODE: String(fd.get('DITHER_MODE') || 'floyd'),
      THRESHOLD: Number(thresholdRange.value),
      FIT_MODE: String(fd.get('FIT_MODE') || 'contain'),
      ROTATE: Number(fd.get('ROTATE') || 0),
    };
  };

  const fetchJson = async (url, options = {}) => {
    const resp = await fetch(url, options);
    const rawText = await resp.text();
    let data = {};
    if (rawText) {
      try {
        data = JSON.parse(rawText);
      } catch {
        throw new Error(`响应格式无效 (${resp.status})`);
      }
    }
    if (!resp.ok || data.ok === false) {
      const err = new Error(data.error || `请求失败 (${resp.status})`);
      err.fields = data.fields || {};
      throw err;
    }
    return data;
  };

  const refreshHealth = async () => {
    if (healthInFlight) return;
    healthInFlight = true;
    try {
      const health = await fetchJson('/health');
      currentPaused = Boolean(health.paused);
      serviceBadge.textContent = currentPaused ? '已暂停推流' : '后台运行中';
      serviceBadge.classList.toggle('ok', !currentPaused);
      serviceBadge.classList.toggle('paused', currentPaused);
      togglePauseBtn.textContent = currentPaused ? '恢复推流' : '暂停推流';

      const w = Number(health.config?.SCREEN_WIDTH || 400);
      const h = Number(health.config?.SCREEN_HEIGHT || 300);
      if (w > 0 && h > 0) {
        previewImg.style.aspectRatio = `${w} / ${h}`;
      }

      // 只要外部服务（如 Quote0Local）或后台重算导致 frame_seq/sha 变化，立即更新网页预览图
      const previewKey = health.has_frame ? `${health.frame_seq ?? 0}:${health.frame_sha256 || ''}` : '';
      if (previewKey && previewKey !== lastPreviewKey) {
        lastPreviewKey = previewKey;
        refreshPreviewImage(previewKey);
      }

      metaSha.textContent = `帧哈希：${health.frame_sha256 ? health.frame_sha256.slice(0, 12) : '暂无'}`;
      const rxTime = health.last_received_at || '--';
      const msText = health.stats.last_push_ms !== null ? `${health.stats.last_push_ms} ms` : '--';
      metaPushInfo.textContent = `收图：${rxTime} · 推设备：${msText}`;

      document.getElementById('stat-pushed').textContent = String(health.stats.pushed_frames ?? 0);
      document.getElementById('stat-skipped').textContent = String(health.stats.skipped_frames ?? 0);
      document.getElementById('stat-heartbeats').textContent = String(health.stats.heartbeats_sent ?? 0);
      document.getElementById('stat-failed').textContent = String(health.stats.failed_pushes ?? 0);

      const errEl = document.getElementById('last-error-text');
      if (health.stats.last_error) {
        errEl.textContent = `最近异常：${health.stats.last_error}`;
        errEl.hidden = false;
      } else {
        errEl.hidden = true;
      }
    } catch (err) {
      serviceBadge.textContent = '服务未响应';
      serviceBadge.classList.remove('ok');
      serviceBadge.classList.add('paused');
    } finally {
      healthInFlight = false;
    }
  };

  const refreshDeviceStatus = async () => {
    if (deviceInFlight) return;
    deviceInFlight = true;
    try {
      const data = await fetchJson('/api/device-status');
      if (data.device) {
        const d = data.device;
        document.getElementById('dev-mode').textContent = String(d.mode || '--');
        document.getElementById('dev-streaming').textContent = d.streaming ? '投屏中' : '等待连接';
        document.getElementById('dev-fps-bat').textContent = `${d.fps ?? 0} FPS / ${d.battery ?? '--'}%`;
        document.getElementById('dev-rssi').textContent = `${d.rssi ?? '--'} dBm`;
      } else {
        document.getElementById('dev-mode').textContent = '离线';
        document.getElementById('dev-streaming').textContent = data.error || '未连接';
        document.getElementById('dev-fps-bat').textContent = '--';
        document.getElementById('dev-rssi').textContent = '--';
      }
    } catch (err) {
      document.getElementById('dev-mode').textContent = '离线';
      document.getElementById('dev-streaming').textContent = err.message || '未连接';
      document.getElementById('dev-fps-bat').textContent = '--';
      document.getElementById('dev-rssi').textContent = '--';
    } finally {
      deviceInFlight = false;
    }
  };

  const refreshHealthAndDevice = async () => {
    await Promise.allSettled([refreshHealth(), refreshDeviceStatus()]);
  };

  const uploadFile = async (file) => {
    if (!file) {
      showStatus('请先选择要推送的图片文件。', true);
      return;
    }
    try {
      const fd = new FormData();
      fd.append('image', file);
      const prevKey = lastPreviewKey;
      const result = await fetchJson('/push?push_now=1', { method: 'POST', body: fd });
      showStatus(`图片已转换（${result.width}×${result.height}，${result.frame_bytes} 字节）并触发推送。`);
      await refreshHealth();
      if (lastPreviewKey === prevKey) {
        refreshPreviewImage(lastPreviewKey);
      }
      void refreshDeviceStatus();
    } catch (err) {
      showStatus(`上传推送失败：${err.message}`, true);
    }
  };

  const runControlAction = async (action) => {
    try {
      const prevKey = lastPreviewKey;
      const res = await fetchJson('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      if (action === 'test_pattern') {
        showStatus('已生成测试图案并触发设备刷新。');
      } else if (action === 'push_now') {
        showStatus('已触发立即向设备推流一帧。');
      } else if (action === 'pause' || action === 'resume') {
        showStatus(res.paused ? '后台定时推流已暂停。' : '后台定时推流已恢复。');
      } else if (action === 'disconnect') {
        showStatus('已发送断开指令，设备回到第四屏等待连接状态。');
      }
      await refreshHealth();
      if (action === 'test_pattern' && lastPreviewKey === prevKey) {
        refreshPreviewImage(lastPreviewKey);
      }
      void refreshDeviceStatus();
    } catch (err) {
      showStatus(`执行控制失败：${err.message}`, true);
    }
  };

  /* 表单提交：校验字段，保存至 .env 并更新模拟预览 */
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    form.querySelectorAll('input').forEach((el) => applyCustomConstraint(el));

    if (!form.checkValidity()) {
      form.reportValidity();
      const firstInvalid = form.querySelector(':invalid');
      if (firstInvalid) firstInvalid.focus();
      showStatus('配置存在格式错误，请按提示修正高亮字段。', true);
      return;
    }

    const saveBtn = document.getElementById('btn-save-config');
    saveBtn.disabled = true;
    try {
      const prevKey = lastPreviewKey;
      const payload = collectConfigPayload();
      const res = await fetchJson('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      fillConfigForm(res.config);
      showStatus('配置已保存到 .env，预览图像已同步重算。');
      await refreshHealth();
      if (lastPreviewKey === prevKey) {
        refreshPreviewImage(lastPreviewKey);
      }
      void refreshDeviceStatus();
    } catch (err) {
      showStatus(`保存失败：${err.message}`, true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  thresholdRange.addEventListener('input', () => {
    thresholdVal.textContent = thresholdRange.value;
  });

  toggleTokenBtn.addEventListener('click', () => {
    const showing = tokenInput.type === 'text';
    tokenInput.type = showing ? 'password' : 'text';
    toggleTokenBtn.setAttribute('aria-pressed', showing ? 'false' : 'true');
    toggleTokenBtn.textContent = showing ? '显示' : '隐藏';
  });

  document.getElementById('btn-test-pattern').addEventListener('click', () => runControlAction('test_pattern'));
  document.getElementById('btn-push-now').addEventListener('click', () => runControlAction('push_now'));
  document.getElementById('btn-toggle-pause').addEventListener('click', () =>
    runControlAction(currentPaused ? 'resume' : 'pause')
  );
  document.getElementById('btn-disconnect').addEventListener('click', () => runControlAction('disconnect'));
  document.getElementById('btn-refresh-status').addEventListener('click', refreshHealthAndDevice);

  document.getElementById('btn-upload-push').addEventListener('click', () => {
    uploadFile(fileInput.files?.[0]);
  });

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-over');
  });
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const dropped = e.dataTransfer?.files?.[0];
    if (dropped) uploadFile(dropped);
  });

  document.getElementById('btn-copy-curl').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(curlBox.textContent);
      showStatus('curl 接入示例已复制到剪贴板。');
    } catch {
      showStatus('未能自动复制，请手动全选示例文本。', true);
    }
  });

  const startPolling = () => {
    if (healthPollTimer) clearInterval(healthPollTimer);
    if (devicePollTimer) clearInterval(devicePollTimer);
    // 每 500 ms 轻量轮询本地 /health 检测外部新推帧，保证网页实时看到推送画面
    healthPollTimer = setInterval(() => {
      if (document.visibilityState === 'visible') {
        void refreshHealth();
      }
    }, 500);
    // 每 2000 ms 轮询一次设备状态，减少 ESP32 Wi-Fi 查询压力
    devicePollTimer = setInterval(() => {
      if (document.visibilityState === 'visible') {
        void refreshDeviceStatus();
      }
    }, 2000);
  };

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      void refreshHealth();
      void refreshDeviceStatus();
    }
  });

  const boot = async () => {
    initFieldValidation();
    try {
      const cfgResp = await fetchJson('/api/config');
      fillConfigForm(cfgResp.config);
      await refreshHealthAndDevice();
    } catch (err) {
      showStatus(`初始化控台失败：${err.message}`, true);
    }
    startPolling();
  };

  boot();
})();
