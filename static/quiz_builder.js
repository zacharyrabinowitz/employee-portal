(function () {
  function updateQuestionTypeUI(qform) {
    if (!qform) return;
    var typeSelect = qform.querySelector(".q-type-select");
    if (!typeSelect) return;
    var type = typeSelect.value;
    var choicesBox = qform.querySelector(".q-choices");
    var textBox = qform.querySelector(".q-text-answer");
    var matchingBox = qform.querySelector(".q-matching");

    var boxes = [
      { el: choicesBox, active: type !== "text" && type !== "matching" },
      { el: textBox, active: type === "text" },
      { el: matchingBox, active: type === "matching" },
    ];
    boxes.forEach(function (box) {
      if (!box.el) return;
      box.el.style.display = box.active ? "" : "none";
      box.el.querySelectorAll("input").forEach(function (el) {
        el.disabled = !box.active;
      });
    });

    if (type !== "text" && type !== "matching" && choicesBox) {
      choicesBox.querySelectorAll(".q-choice-row").forEach(function (row) {
        var radio = row.querySelector(".q-correct-radio");
        var checkbox = row.querySelector(".q-correct-checkbox");
        if (type === "multi_choice") {
          if (radio) {
            radio.style.display = "none";
            radio.disabled = true;
          }
          if (checkbox) {
            checkbox.style.display = "";
            checkbox.disabled = false;
          }
        } else {
          if (radio) {
            radio.style.display = "";
            radio.disabled = false;
          }
          if (checkbox) {
            checkbox.style.display = "none";
            checkbox.disabled = true;
          }
        }
      });
    }
  }

  function renumberChoices(choicesBox) {
    var rows = choicesBox.querySelectorAll(".q-choice-row");
    rows.forEach(function (row, idx) {
      var radio = row.querySelector(".q-correct-radio");
      var checkbox = row.querySelector(".q-correct-checkbox");
      var text = row.querySelector(".q-choice-text");
      if (radio) radio.value = idx;
      if (checkbox) checkbox.value = idx;
      if (text) text.placeholder = "Choice " + (idx + 1);
    });
  }

  function addChoiceRow(choicesBox) {
    var rows = choicesBox.querySelectorAll(".q-choice-row");
    var last = rows[rows.length - 1];
    var clone = last.cloneNode(true);
    clone.querySelectorAll('input[type="text"]').forEach(function (el) {
      el.value = "";
    });
    clone.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach(function (el) {
      el.checked = false;
    });
    last.insertAdjacentElement("afterend", clone);
    renumberChoices(choicesBox);
  }

  function removeChoiceRow(row) {
    var choicesBox = row.closest(".q-choices");
    var rows = choicesBox.querySelectorAll(".q-choice-row");
    if (rows.length <= 2) return;
    row.remove();
    renumberChoices(choicesBox);
  }

  function addPairRow(matchingBox) {
    var rows = matchingBox.querySelectorAll(".q-pair-row");
    var last = rows[rows.length - 1];
    var clone = last.cloneNode(true);
    clone.querySelectorAll('input[type="text"]').forEach(function (el) {
      el.value = "";
    });
    last.insertAdjacentElement("afterend", clone);
  }

  function removePairRow(row) {
    var matchingBox = row.closest(".q-matching");
    var rows = matchingBox.querySelectorAll(".q-pair-row");
    if (rows.length <= 2) return;
    row.remove();
  }

  var nextQuestionIndex = 0;

  function renumberQuestionLabels() {
    var container = document.getElementById("questions-container");
    if (!container) return;
    container.querySelectorAll(".qblock").forEach(function (block, idx) {
      var label = block.querySelector(".qblock-number");
      if (label) label.textContent = "Question " + (idx + 1);
    });
  }

  function addQuestionBlock() {
    var container = document.getElementById("questions-container");
    var tplEl = document.getElementById("question-template");
    if (!container || !tplEl) return;
    var idx = nextQuestionIndex++;
    var html = tplEl.innerHTML.split("__IDX__").join(String(idx));
    var wrapper = document.createElement("div");
    wrapper.innerHTML = html.trim();
    var block = wrapper.firstElementChild;
    container.appendChild(block);
    updateQuestionTypeUI(block);
    renumberQuestionLabels();
  }

  function removeQuestionBlock(block) {
    block.remove();
    renumberQuestionLabels();
  }

  document.addEventListener("change", function (e) {
    if (e.target.classList.contains("q-type-select")) {
      updateQuestionTypeUI(e.target.closest(".qform"));
    }
  });

  document.addEventListener("click", function (e) {
    if (e.target.classList.contains("q-add-choice")) {
      e.preventDefault();
      addChoiceRow(e.target.closest(".q-choices"));
    } else if (e.target.classList.contains("q-remove-choice")) {
      e.preventDefault();
      removeChoiceRow(e.target.closest(".q-choice-row"));
    } else if (e.target.classList.contains("q-add-pair")) {
      e.preventDefault();
      addPairRow(e.target.closest(".q-matching"));
    } else if (e.target.classList.contains("q-remove-pair")) {
      e.preventDefault();
      removePairRow(e.target.closest(".q-pair-row"));
    } else if (e.target.id === "add-question-btn") {
      e.preventDefault();
      addQuestionBlock();
    } else if (e.target.classList.contains("q-remove-question")) {
      e.preventDefault();
      removeQuestionBlock(e.target.closest(".qblock"));
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".q-type-select").forEach(function (sel) {
      updateQuestionTypeUI(sel.closest(".qform"));
    });
    if (document.getElementById("questions-container") && document.getElementById("question-template")) {
      addQuestionBlock();
    }
  });
})();
