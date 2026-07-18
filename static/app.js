function renderSlideCaptionFields(input, containerId) {
  var container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  Array.prototype.forEach.call(input.files, function (file) {
    var group = document.createElement('div');
    group.className = 'form-group';

    var label = document.createElement('label');
    label.textContent = 'Caption for "' + file.name + '" (optional)';

    var textarea = document.createElement('textarea');
    textarea.name = 'slide_captions';
    textarea.className = 'textarea-caption-inline';
    textarea.placeholder = 'Add a caption for this slide...';

    group.appendChild(label);
    group.appendChild(textarea);
    container.appendChild(group);
  });
}

document.querySelectorAll('.tabs').forEach(function (root) {
  var buttons = root.querySelectorAll('.tab-btn');
  var panels = root.querySelectorAll('.tab-panel');

  function activate(name) {
    buttons.forEach(function (b) { b.classList.toggle('active', b.dataset.tab === name); });
    panels.forEach(function (p) { p.classList.toggle('active', p.dataset.tab === name); });
  }

  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      activate(b.dataset.tab);
      if (history.replaceState) history.replaceState(null, '', '#' + b.dataset.tab);
    });
  });

  var hashTab = window.location.hash ? window.location.hash.slice(1) : null;
  var hasHashTab = hashTab && root.querySelector('.tab-btn[data-tab="' + hashTab + '"]');
  if (hasHashTab) activate(hashTab);
});
