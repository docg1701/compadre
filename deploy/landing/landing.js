(function() {
  'use strict';

  var form = document.getElementById('form');
  var apiKeyEl = document.getElementById('apiKey');
  var audioFileEl = document.getElementById('audioFile');
  var fileUploadArea = document.getElementById('fileUploadArea');
  var fileNameEl = document.getElementById('fileName');
  var responseFormatEl = document.getElementById('responseFormat');
  var languageEl = document.getElementById('language');
  var submitBtn = document.getElementById('submit');
  var errorBox = document.getElementById('error');
  var errorType = document.getElementById('errorType');
  var errorMessage = document.getElementById('errorMessage');
  var errorHint = document.getElementById('errorHint');
  var resultBox = document.getElementById('result');
  var transcriptionText = document.getElementById('transcriptionText');
  var transcriptionMeta = document.getElementById('transcriptionMeta');
  var copyBtn = document.getElementById('copyBtn');

  var COPY_SVG = '<svg viewBox="0 0 24 24"><path d="M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z"/></svg> Copiar';
  var COPIED_SVG = '<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg> Copiado!';

  // ---- Helper: render transcription meta safely (no innerHTML with data) ----

  function clearMeta() {
    while (transcriptionMeta.firstChild) {
      transcriptionMeta.removeChild(transcriptionMeta.firstChild);
    }
  }

  function buildMetaSpan(label, value) {
    var span = document.createElement('span');
    span.textContent = label + ': ';
    var strong = document.createElement('strong');
    strong.textContent = String(value);
    span.appendChild(strong);
    return span;
  }

  function renderMeta(items) {
    clearMeta();
    items.forEach(function(item) {
      transcriptionMeta.appendChild(buildMetaSpan(item.label, item.value));
    });
  }

  // ---- File upload drag-and-drop ----

  fileUploadArea.addEventListener('dragover', function(e) {
    e.preventDefault();
    fileUploadArea.classList.add('dragover');
  });

  fileUploadArea.addEventListener('dragleave', function() {
    fileUploadArea.classList.remove('dragover');
  });

  fileUploadArea.addEventListener('drop', function(e) {
    e.preventDefault();
    fileUploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
      audioFileEl.files = e.dataTransfer.files;
      updateFileDisplay();
    }
  });

  audioFileEl.addEventListener('change', updateFileDisplay);

  function updateFileDisplay() {
    var file = audioFileEl.files[0];
    if (file) {
      fileNameEl.textContent = file.name;
      fileUploadArea.classList.add('has-file');
    } else {
      fileNameEl.textContent = '';
      fileUploadArea.classList.remove('has-file');
    }
  }

  // ---- Error display (all .textContent — safe) ----

  function showError(type, message, hint) {
    errorType.textContent = type || 'error';
    errorMessage.textContent = message || 'Erro desconhecido';
    errorHint.textContent = hint || '';
    errorHint.style.display = hint ? 'block' : 'none';
    errorBox.classList.add('visible');
  }

  function hideError() {
    errorBox.classList.remove('visible');
  }

  // ---- Result panel ----

  function hideResult() {
    resultBox.classList.remove('visible');
    transcriptionText.textContent = '';
    clearMeta();
    resetCopyButton();
  }

  function resetCopyButton() {
    copyBtn.classList.remove('copied');
    copyBtn.innerHTML = COPY_SVG;
  }

  // ---- Copy to clipboard ----

  copyBtn.addEventListener('click', function() {
    var text = transcriptionText.textContent;
    if (!text) return;

    try {
      awaitNavigatorClipboard(text);
    } catch (_e) {
      fallbackCopy(text);
    }
  });

  async function awaitNavigatorClipboard(text) {
    await navigator.clipboard.writeText(text);
    copyBtn.classList.add('copied');
    copyBtn.innerHTML = COPIED_SVG;
    setTimeout(resetCopyButton, 2500);
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    copyBtn.classList.add('copied');
    copyBtn.innerHTML = COPIED_SVG;
    setTimeout(resetCopyButton, 2500);
  }

  // ---- Submit handler ----

  form.addEventListener('submit', function(e) {
    e.preventDefault();
    hideError();
    hideResult();

    var apiKey = apiKeyEl.value.trim();
    if (!apiKey) {
      showError('invalid_request', 'API Key é obrigatória.', 'Cole sua chave no campo acima.');
      return;
    }

    var file = audioFileEl.files[0];
    if (!file) {
      showError('invalid_request', 'Selecione um arquivo de áudio.', 'Clique ou arraste um arquivo para a área de upload.');
      return;
    }

    var formData = new FormData();
    formData.append('file', file);
    formData.append('model', 'faster-whisper-large-v3');
    formData.append('response_format', responseFormatEl.value);
    formData.append('language', languageEl.value.trim() || 'pt');

    submitBtn.disabled = true;
    submitBtn.textContent = 'Transcrevendo\u2026';

    var ac = new AbortController();
    setTimeout(function() { ac.abort(); }, 120000);

    fetch('/v1/audio/transcriptions', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + apiKey,
      },
      body: formData,
      signal: ac.signal,
    })
      .then(function(r) {
        if (!r.ok) {
          return r.json()
            .catch(function() {
              return { error: { message: 'HTTP ' + r.status, type: 'http_error', hint: '' } };
            })
            .then(function(errData) {
              var err = errData.error || { message: 'HTTP ' + r.status, type: 'unknown', hint: '' };
              showError(err.type, err.message, err.hint);
            });
        }

        var respFormat = responseFormatEl.value;

        if (respFormat === 'text') {
          return r.text().then(function(t) { renderText(t, respFormat); });
        }
        if (respFormat === 'srt' || respFormat === 'vtt') {
          return r.text().then(function(t) { renderText(t, respFormat); });
        }
        // json or verbose_json
        return r.json().then(function(data) {
          if (respFormat === 'verbose_json') {
            renderVerboseJson(data);
          } else {
            renderText(data.text || '', respFormat);
          }
        });
      })
      .catch(function(e) {
        showError('network_error', e.message || 'Erro de rede', 'Verifique sua conex\u00e3o e tente novamente.');
      })
      .finally(function() {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Transcrever';
      });
  });

  function renderText(text, format) {
    transcriptionText.textContent = text;
    renderMeta([{ label: 'Formato', value: format }]);
    resultBox.classList.add('visible');
  }

  function renderVerboseJson(data) {
    transcriptionText.textContent = data.text || '';
    var lang = data.language || '\u2014';
    var duration = data.duration != null ? Number(data.duration).toFixed(1) + 's' : '\u2014';
    var segments = (data.segments || []).length;
    renderMeta([
      { label: 'Idioma detectado', value: lang },
      { label: 'Dura\u00e7\u00e3o', value: duration },
      { label: 'Segmentos', value: segments }
    ]);
    resultBox.classList.add('visible');
  }
})();
