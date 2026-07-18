(function () {
  var canvas = document.getElementById('editorCanvas');
  if (!canvas) return;

  var slideId = canvas.dataset.slideId;
  var selected = null;

  var panel = {
    noSelection: document.getElementById('noSelectionMsg'),
    props: document.getElementById('elementProps'),
    textGroup: document.getElementById('textContentGroup'),
    fontSizeGroup: document.getElementById('fontSizeGroup'),
    colorGroup: document.getElementById('colorGroup'),
    boldGroup: document.getElementById('boldGroup'),
    alignGroup: document.getElementById('alignGroup'),
    text: document.getElementById('elText'),
    fontSize: document.getElementById('elFontSize'),
    color: document.getElementById('elColor'),
    bold: document.getElementById('elBold'),
    alignBtns: document.querySelectorAll('.align-btn'),
    bringToFront: document.getElementById('bringToFrontBtn'),
    deleteBtn: document.getElementById('deleteElementBtn')
  };

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function postForm(url, formData) {
    return fetch(url, { method: 'POST', body: formData }).then(function (r) { return r.json(); });
  }

  function updateElementUrl(id) {
    return '/admin/training/slides/elements/' + id + '/update';
  }

  // ---------------- Selection / properties panel ----------------

  function selectElement(el) {
    if (selected) selected.classList.remove('selected');
    selected = el;
    if (!selected) {
      panel.noSelection.style.display = '';
      panel.props.style.display = 'none';
      return;
    }
    selected.classList.add('selected');
    panel.noSelection.style.display = 'none';
    panel.props.style.display = '';

    var isText = selected.dataset.type === 'text';
    panel.textGroup.style.display = isText ? '' : 'none';
    panel.fontSizeGroup.style.display = isText ? '' : 'none';
    panel.colorGroup.style.display = isText ? '' : 'none';
    panel.boldGroup.style.display = isText ? '' : 'none';
    panel.alignGroup.style.display = isText ? '' : 'none';

    if (isText) {
      var contentEl = selected.querySelector('.el-text-content');
      panel.text.value = contentEl ? contentEl.textContent : '';
      panel.fontSize.value = selected.dataset.fontSize || 18;
      panel.color.value = selected.dataset.color || '#1f2430';
      panel.bold.checked = selected.dataset.bold === '1';
      var align = selected.dataset.align || 'left';
      panel.alignBtns.forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.align === align);
      });
    }
  }

  document.addEventListener('click', function (e) {
    if (!canvas.contains(e.target)) {
      selectElement(null);
    }
  });

  // ---------------- Drag + resize ----------------

  function attachElementHandlers(el) {
    el.addEventListener('mousedown', function (e) {
      if (e.target.classList.contains('resize-handle')) return;
      e.preventDefault();
      selectElement(el);

      var rect = canvas.getBoundingClientRect();
      var startX = e.clientX;
      var startY = e.clientY;
      var startLeft = parseFloat(el.dataset.posX);
      var startTop = parseFloat(el.dataset.posY);

      function onMove(ev) {
        var dxPct = ((ev.clientX - startX) / rect.width) * 100;
        var dyPct = ((ev.clientY - startY) / rect.height) * 100;
        var newLeft = Math.max(0, Math.min(100, startLeft + dxPct));
        var newTop = Math.max(0, Math.min(100, startTop + dyPct));
        el.style.left = newLeft + '%';
        el.style.top = newTop + '%';
        el.dataset.posX = newLeft;
        el.dataset.posY = newTop;
      }

      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        postJSON(updateElementUrl(el.dataset.elementId), {
          pos_x: parseFloat(el.dataset.posX),
          pos_y: parseFloat(el.dataset.posY)
        });
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    var handle = el.querySelector('.resize-handle');
    if (handle) {
      handle.addEventListener('mousedown', function (e) {
        e.preventDefault();
        e.stopPropagation();
        selectElement(el);

        var rect = canvas.getBoundingClientRect();
        var startX = e.clientX;
        var startY = e.clientY;
        var startWidth = parseFloat(el.dataset.width);
        var startHeight = parseFloat(el.dataset.height);

        function onMove(ev) {
          var dwPct = ((ev.clientX - startX) / rect.width) * 100;
          var dhPct = ((ev.clientY - startY) / rect.height) * 100;
          var newWidth = Math.max(4, Math.min(100, startWidth + dwPct));
          var newHeight = Math.max(4, Math.min(100, startHeight + dhPct));
          el.style.width = newWidth + '%';
          el.style.height = newHeight + '%';
          el.dataset.width = newWidth;
          el.dataset.height = newHeight;
        }

        function onUp() {
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          postJSON(updateElementUrl(el.dataset.elementId), {
            width: parseFloat(el.dataset.width),
            height: parseFloat(el.dataset.height)
          });
        }

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    }
  }

  canvas.querySelectorAll('.canvas-el').forEach(attachElementHandlers);

  // ---------------- Properties panel -> element ----------------

  var textDebounce = null;
  panel.text.addEventListener('input', function () {
    if (!selected) return;
    var contentEl = selected.querySelector('.el-text-content');
    if (contentEl) contentEl.textContent = panel.text.value;
    clearTimeout(textDebounce);
    textDebounce = setTimeout(function () {
      postJSON(updateElementUrl(selected.dataset.elementId), { content: panel.text.value });
    }, 400);
  });

  panel.fontSize.addEventListener('change', function () {
    if (!selected) return;
    var val = parseInt(panel.fontSize.value, 10) || 18;
    selected.style.fontSize = val + 'px';
    selected.dataset.fontSize = val;
    postJSON(updateElementUrl(selected.dataset.elementId), { font_size: val });
  });

  panel.color.addEventListener('input', function () {
    if (!selected) return;
    selected.style.color = panel.color.value;
    selected.dataset.color = panel.color.value;
    postJSON(updateElementUrl(selected.dataset.elementId), { color: panel.color.value });
  });

  panel.bold.addEventListener('change', function () {
    if (!selected) return;
    selected.style.fontWeight = panel.bold.checked ? 'bold' : 'normal';
    selected.dataset.bold = panel.bold.checked ? '1' : '0';
    postJSON(updateElementUrl(selected.dataset.elementId), { bold: panel.bold.checked });
  });

  panel.alignBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (!selected) return;
      var align = btn.dataset.align;
      selected.style.textAlign = align;
      selected.dataset.align = align;
      panel.alignBtns.forEach(function (b) { b.classList.toggle('active', b === btn); });
      postJSON(updateElementUrl(selected.dataset.elementId), { align: align });
    });
  });

  panel.bringToFront.addEventListener('click', function () {
    if (!selected) return;
    postJSON('/admin/training/slides/elements/' + selected.dataset.elementId + '/bring-to-front', {})
      .then(function (res) {
        if (res && res.ok) {
          selected.style.zIndex = res.z_index;
          selected.dataset.zIndex = res.z_index;
        }
      });
  });

  panel.deleteBtn.addEventListener('click', function () {
    if (!selected) return;
    if (!confirm('Delete this element?')) return;
    var id = selected.dataset.elementId;
    var node = selected;
    postJSON('/admin/training/slides/elements/' + id + '/delete', {}).then(function (res) {
      if (res && res.ok) {
        node.remove();
        selectElement(null);
      }
    });
  });

  // ---------------- Toolbar: add text / add image ----------------

  document.getElementById('addTextBtn').addEventListener('click', function () {
    postJSON('/admin/training/slides/' + slideId + '/elements/text', {}).then(function (res) {
      if (!res || !res.ok) return;
      var el = res.element;
      var div = document.createElement('div');
      div.className = 'canvas-el canvas-el-text';
      div.dataset.elementId = el.id;
      div.dataset.type = 'text';
      div.dataset.posX = el.pos_x;
      div.dataset.posY = el.pos_y;
      div.dataset.width = el.width;
      div.dataset.height = el.height;
      div.dataset.zIndex = el.z_index;
      div.dataset.fontSize = el.font_size;
      div.dataset.color = el.color;
      div.dataset.bold = el.bold;
      div.dataset.align = el.align;
      div.style.left = el.pos_x + '%';
      div.style.top = el.pos_y + '%';
      div.style.width = el.width + '%';
      div.style.height = el.height + '%';
      div.style.zIndex = el.z_index;
      div.style.fontSize = el.font_size + 'px';
      div.style.color = el.color;
      div.style.fontWeight = el.bold ? 'bold' : 'normal';
      div.style.textAlign = el.align;

      var content = document.createElement('span');
      content.className = 'el-text-content';
      content.textContent = el.content;
      var handle = document.createElement('span');
      handle.className = 'resize-handle';

      div.appendChild(content);
      div.appendChild(handle);
      canvas.appendChild(div);
      attachElementHandlers(div);
      selectElement(div);
    });
  });

  var imageFileInput = document.getElementById('imageFileInput');
  document.getElementById('addImageBtn').addEventListener('click', function () {
    imageFileInput.click();
  });

  imageFileInput.addEventListener('change', function () {
    if (!imageFileInput.files.length) return;
    var formData = new FormData();
    formData.append('image_file', imageFileInput.files[0]);
    postForm('/admin/training/slides/' + slideId + '/elements/image', formData).then(function (res) {
      imageFileInput.value = '';
      if (!res || !res.ok) {
        alert('Could not add that image.');
        return;
      }
      var el = res.element;
      var div = document.createElement('div');
      div.className = 'canvas-el';
      div.dataset.elementId = el.id;
      div.dataset.type = 'image';
      div.dataset.posX = el.pos_x;
      div.dataset.posY = el.pos_y;
      div.dataset.width = el.width;
      div.dataset.height = el.height;
      div.dataset.zIndex = el.z_index;
      div.style.left = el.pos_x + '%';
      div.style.top = el.pos_y + '%';
      div.style.width = el.width + '%';
      div.style.height = el.height + '%';
      div.style.zIndex = el.z_index;

      var img = document.createElement('img');
      img.src = res.element.image_url;
      var handle = document.createElement('span');
      handle.className = 'resize-handle';

      div.appendChild(img);
      div.appendChild(handle);
      canvas.appendChild(div);
      attachElementHandlers(div);
      selectElement(div);
    });
  });

  // ---------------- Slide background ----------------

  var bgInput = document.getElementById('bgColorInput');
  bgInput.addEventListener('input', function () {
    canvas.style.backgroundColor = bgInput.value;
  });
  bgInput.addEventListener('change', function () {
    postJSON('/admin/training/slides/' + slideId + '/background', { background_color: bgInput.value });
  });

  // ---------------- Filmstrip drag-to-reorder ----------------

  var filmstrip = document.getElementById('filmstrip');
  var dragged = null;

  if (filmstrip) {
    filmstrip.querySelectorAll('.filmstrip-thumb').forEach(function (thumb) {
      thumb.addEventListener('dragstart', function () {
        dragged = thumb;
        thumb.classList.add('dragging');
      });
      thumb.addEventListener('dragend', function () {
        thumb.classList.remove('dragging');
        dragged = null;
        var order = Array.prototype.map.call(
          filmstrip.querySelectorAll('.filmstrip-thumb'),
          function (t) { return t.dataset.slideId; }
        );
        var moduleId = filmstrip.dataset.moduleId;
        postJSON('/admin/training/' + moduleId + '/slides/reorder', { order: order });
      });
      thumb.addEventListener('dragover', function (e) {
        e.preventDefault();
        if (!dragged || dragged === thumb) return;
        var rect = thumb.getBoundingClientRect();
        var before = (e.clientX - rect.left) < rect.width / 2;
        filmstrip.insertBefore(dragged, before ? thumb : thumb.nextSibling);
      });
    });
  }
})();
