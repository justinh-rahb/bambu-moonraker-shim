'use strict';
document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', async () => {
    const source = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(source.textContent);
      button.textContent = 'Copied ✓';
      status.textContent = 'Clone commands copied to clipboard.';
      setTimeout(() => { button.textContent = 'Copy ⧉'; }, 2000);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(source);
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = 'Select & copy';
      status.textContent = 'Commands selected. Use your system copy shortcut.';
    }
  });
});
