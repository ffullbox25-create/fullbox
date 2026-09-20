(function () {
  function showError(form, message) {
    var boxes = form.querySelectorAll('.js-errorbox-all');
    if (boxes.length) {
      boxes.forEach(function (box) {
        box.style.display = 'block';
        var item = box.querySelector('.js-rule-error-all');
        if (item) {
          item.textContent = message;
        }
      });
      return;
    }

    var fallback = form.querySelector('.fullbox-form-error');
    if (!fallback) {
      fallback = document.createElement('div');
      fallback.className = 'fullbox-form-error';
      form.appendChild(fallback);
    }
    fallback.textContent = message;
  }

  function clearError(form) {
    form.querySelectorAll('.js-errorbox-all').forEach(function (box) {
      box.style.display = 'none';
      var item = box.querySelector('.js-rule-error-all');
      if (item) {
        item.textContent = '';
      }
    });
    var fallback = form.querySelector('.fullbox-form-error');
    if (fallback) {
      fallback.textContent = '';
    }
  }

  function initLandingForm(form) {
    if (!form || form.dataset.fullboxBound === '1') {
      return;
    }
    form.dataset.fullboxBound = '1';
    form.action = '/landing-submit/';

    form.addEventListener(
      'submit',
      function (event) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (form.dataset.fullboxSubmitting === '1') {
          return;
        }

        clearError(form);
        form.dataset.fullboxSubmitting = '1';

        var payload = new FormData(form);
        payload.append('_page', window.location.pathname);
        payload.append('_form_id', form.id || '');
        if (!payload.get('_redirect')) {
          payload.append('_redirect', '/spasibo');
        }

        fetch('/landing-submit/', {
          method: 'POST',
          body: payload,
          credentials: 'same-origin',
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
          },
        })
          .then(function (response) {
            return response.json().then(function (data) {
              return { ok: response.ok, data: data };
            });
          })
          .then(function (result) {
            if (!result.ok || !result.data.ok) {
              throw new Error((result.data && result.data.error) || 'Не удалось отправить форму');
            }
            window.location.href = result.data.redirect || '/spasibo';
          })
          .catch(function (error) {
            showError(form, error.message || 'Не удалось отправить форму');
          })
          .finally(function () {
            form.dataset.fullboxSubmitting = '0';
          });
      },
      true
    );
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form.t-form').forEach(initLandingForm);
  });
})();
