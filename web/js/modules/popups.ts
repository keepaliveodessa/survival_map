// modules/popups.js - Управление центральными попапами

/**
 * Показывает центральный попап с контентом
 * @param {string} content - HTML контент попапа
 */
function showCenterPopup(content: string): void {
    const popup = document.getElementById('centerPopup');
    const overlay = document.getElementById('centerPopupOverlay');
    const popupContent = document.getElementById('centerPopupContent');

    if (popupContent) popupContent.innerHTML = content;
    popup?.classList.add('show');
    overlay?.classList.add('show');
}

/**
 * Скрывает центральный попап
 */
function hideCenterPopup(): void {
    const popup = document.getElementById('centerPopup');
    const overlay = document.getElementById('centerPopupOverlay');

    popup?.classList.remove('show');
    overlay?.classList.remove('show');
}

/**
 * Копирует текст в буфер обмена
 * @param {string} text - Текст для копирования
 * @returns {Promise<boolean>} - Успешно ли скопировано
 */
async function copyToClipboard(text: string): Promise<boolean> {
    try {
        // Haptic feedback при копировании
        if (window.telegramIntegration) {
            window.telegramIntegration.hapticFeedback('light');
        }

        // ВАЖНО: В Telegram Mini Apps нет прямого доступа к clipboard
        // Используем workaround через textarea + execCommand
        // Источник: https://core.telegram.org/bots/webapps

        if (window.Telegram?.WebApp) {
            // Создаем временный textarea для копирования
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.left = '-999999px';
            textarea.style.top = '-999999px';
            textarea.setAttribute('readonly', '');
            document.body.appendChild(textarea);

            // Выделяем текст
            textarea.select();
            textarea.setSelectionRange(0, text.length);

            // Копируем через execCommand (работает в Telegram)
            let success = false;
            try {
                success = document.execCommand('copy');
            } catch (err) {
                console.error('execCommand failed:', err);
            }

            // Удаляем временный элемент
            document.body.removeChild(textarea);

            if (success) {
                // Rule 5: showPopup() fires 'success' haptic itself (wrapper-level
                // guarantee) — no separate hapticFeedback() call needed here.
                if (window.telegramIntegration) {
                    window.telegramIntegration.showPopup('Адрес скопирован в буфер обмена', [{ type: 'ok' }], 'success');
                } else {
                    window.hapticFeedback?.('success');
                    alert('✅ Адрес скопирован в буфер обмена');
                }
                return true;
            } else {
                throw new Error('execCommand copy failed');
            }
        }

        // Fallback для обычного браузера (не Telegram)
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            window.hapticFeedback?.('success');
            alert('✅ Адрес скопирован в буфер обмена');
            return true;
        }

        // Последний fallback через execCommand для старых браузеров
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const success = document.execCommand('copy');
        document.body.removeChild(textarea);

        if (success) {
            window.hapticFeedback?.('success');
            alert('✅ Адрес скопирован в буфер обмена');
            return true;
        }

        throw new Error('All copy methods failed');

    } catch (error) {
        console.error('Copy failed:', error);

        // Rule 5: showAlert() fires 'error' haptic itself (wrapper-level
        // guarantee) — no separate hapticFeedback() call needed here.
        if (window.telegramIntegration) {
            window.telegramIntegration.showAlert('❌ Не удалось скопировать адрес. Попробуйте еще раз.', 'error');
        } else {
            window.hapticFeedback?.('error');
            alert('❌ Не удалось скопировать адрес');
        }
        return false;
    }
}

/**
 * Создает и показывает попап легенды
 */
function showLegendPopup(): void {
    // Порядок строк соответствует приоритету классификации слоёв
    // (parser/layer_classifier.py: bus → cops → traffic → pig-fallback).
    const rowStyle = 'padding: 10px 0; color: var(--tg-text-color, #000); font-size: 15px; line-height: 1.4;';
    const iconCellStyle = 'padding: 10px 12px 10px 0; width: 44px; text-align: center;';
    const imgStyle = 'width: 32px; height: 32px; display: block; margin: 0 auto;';

    const content = `
        <h3 style="text-align: center; margin: 0 0 16px 0; font-size: 18px;">Легенда</h3>
        <table style="border-spacing: 0; width: 100%; margin-bottom: 16px;">
            <tbody>
                <tr>
                    <td style="${iconCellStyle}">
                        <img src="/assets/images/bus.webp" style="${imgStyle}" alt="Bus">
                    </td>
                    <td style="${rowStyle}"><strong>Бус</strong> — бус, вито, спринтер, сталкер...</td>
                </tr>
                <tr>
                    <td style="${iconCellStyle}">
                        <img src="/assets/images/cops.webp" style="${imgStyle}" alt="Cops">
                    </td>
                    <td style="${rowStyle}"><strong>Менты</strong> — менты, патруль, люстра, мусра...</td>
                </tr>
                <tr>
                    <td style="${iconCellStyle} font-size: 28px; line-height: 32px;">⛔</td>
                    <td style="${rowStyle}"><strong>Трафик</strong> — ДТП, пробка, блокпост, авария...</td>
                </tr>
                <tr>
                    <td style="${iconCellStyle}">
                        <img src="/assets/images/pig.webp" style="${imgStyle}" alt="Pig">
                    </td>
                    <td style="${rowStyle}"><strong>Остальное</strong> — события без явного типа</td>
                </tr>
            </tbody>
        </table>
        <p style="margin: 0 0 16px 0; color: var(--tg-hint-color, #888); font-size: 13px; line-height: 1.4; text-align: center;">
            Тип события определяется автоматически по ключевым словам в тексте сообщения
        </p>
        <div style="padding-top: 16px; border-top: 1px solid var(--tg-hint-color, #e0e0e0);">
            <p style="margin: 0 0 10px 0; color: var(--tg-text-color, #000); font-size: 14px; line-height: 1.5;">
                <strong style="color: #dc3545;">● Красный</strong> — до 15 минут
            </p>
            <p style="margin: 0 0 10px 0; color: var(--tg-text-color, #000); font-size: 14px; line-height: 1.5;">
                <strong style="color: #0d6efd;">● Синий</strong> — до 30 минут
            </p>
            <p style="margin: 0 0 10px 0; font-size: 14px; line-height: 1.5;">
                <strong style="color: #999; text-shadow: 0 0 2px rgba(0,0,0,0.3);">● Белый</strong> — до 60 минут
            </p>
            <p style="margin: 0; color: var(--tg-hint-color, #888); font-size: 13px; line-height: 1.4;">
                Круг/контур — точное место; линия — улица
            </p>
        </div>
    `;
    showCenterPopup(content);
}


// Делаем функции глобальными для использования в других модулях
window.showCenterPopup = showCenterPopup;
window.hideCenterPopup = hideCenterPopup;
window.copyToClipboard = copyToClipboard;
window.showLegendPopup = showLegendPopup;
