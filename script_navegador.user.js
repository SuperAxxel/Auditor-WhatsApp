// ==UserScript==
// @name         WhatsApp Web - Botones + Audio Preciso + Imágenes DOM + AutoRecarga (V24.3)
// @namespace    http://tampermonkey.net/
// @version      24.3
// @description  Interceptor nativo restaurado y sistema de auto-recarga funcional.
// @author       Hugo Axxel Mata Márquez
// @match        https://web.whatsapp.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    // ==========================================
    // CONFIGURACIÓN DEL TEMPORIZADOR DE RECARGA
    // ==========================================
    const TIEMPO_ESPERA = 300000; // 5 minutos de inactividad
    const TIEMPO_AVISO = 10;      // Segundos de cuenta regresiva

    let temporizadorInactividad;
    let intervaloCuentaRegresiva;
    let avisoVisual = null;

    // ==========================================
    // 1. LA BÓVEDA E INTERCEPTORES (ORIGINAL Y FUNCIONAL)
    // ==========================================
    // Al usar @grant none, esto corre en el contexto de la página esquivando el CSP
    window._bovedaArchivos = {
        audios: {},
        ultimoAudioReproducido: null
    };

    const creadorOriginal = window.URL.createObjectURL;
    window.URL.createObjectURL = function(objeto) {
        const urlGenerada = creadorOriginal.apply(this, arguments);
        if (objeto instanceof Blob) {
            const tipo = objeto.type.toLowerCase();
            if (tipo.includes('audio') || tipo.includes('ogg')) {
                window._bovedaArchivos.audios[urlGenerada] = objeto;
            }
        }
        return urlGenerada;
    };

    const playOriginal = HTMLAudioElement.prototype.play;
    HTMLAudioElement.prototype.play = function() {
        if (this.src && this.src.startsWith('blob:')) {
            window._bovedaArchivos.ultimoAudioReproducido = this.src;
            console.log("Audio detectado y fijado como objetivo:", this.src);
        }
        return playOriginal.apply(this, arguments);
    };


    // ==========================================
    // --- SISTEMA DE CONTROL DE INACTIVIDAD ---
    // ==========================================

    function crearAvisoVisual() {
        if (avisoVisual) return;

        avisoVisual = document.createElement('div');
        avisoVisual.style.position = 'fixed';
        avisoVisual.style.top = '50%';
        avisoVisual.style.left = '50%';
        avisoVisual.style.transform = 'translate(-50%, -50%)';
        avisoVisual.style.backgroundColor = 'rgba(20, 20, 20, 0.95)';
        avisoVisual.style.color = '#fff';
        avisoVisual.style.padding = '30px 40px';
        avisoVisual.style.borderRadius = '12px';
        avisoVisual.style.zIndex = '99999999';
        avisoVisual.style.boxShadow = '0 10px 30px rgba(0,0,0,0.5)';
        avisoVisual.style.fontFamily = 'Segoe UI, Helvetica Neue, Arial, sans-serif';
        avisoVisual.style.textAlign = 'center';
        avisoVisual.style.border = '2px solid #d32f2f';

        document.body.appendChild(avisoVisual);
    }

    function destruirAvisoVisual() {
        if (avisoVisual && avisoVisual.parentNode) {
            avisoVisual.parentNode.removeChild(avisoVisual);
        }
        avisoVisual = null;
    }

    function iniciarCuentaRegresiva() {
        clearInterval(intervaloCuentaRegresiva);
        crearAvisoVisual();

        let segundosRestantes = TIEMPO_AVISO;
        avisoVisual.innerHTML = `<h3 style="margin:0 0 10px 0; color:#ef5350;">⚠️ Alerta de Inactividad</h3>
                                 <p style="margin:0;">WhatsApp Web se recargará en <strong style="font-size:1.3em; color:#ef5350;">${segundosRestantes}</strong> segundos.</p>
                                 <p style="margin:10px 0 0 0; font-size:0.85em; color:#aaa;">Mueve el mouse o presiona una tecla para cancelar.</p>`;

        intervaloCuentaRegresiva = setInterval(() => {
            segundosRestantes--;
            if (segundosRestantes <= 0) {
                clearInterval(intervaloCuentaRegresiva);
                location.reload();
            } else if (avisoVisual) {
                avisoVisual.querySelector('strong').innerText = segundosRestantes;
            }
        }, 1000);
    }

    function reiniciarTemporizador() {
        clearTimeout(temporizadorInactividad);
        clearInterval(intervaloCuentaRegresiva);
        destruirAvisoVisual();

        if (!document.hidden) {
            temporizadorInactividad = setTimeout(iniciarCuentaRegresiva, TIEMPO_ESPERA);
        }
    }

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            clearTimeout(temporizadorInactividad);
            clearInterval(intervaloCuentaRegresiva);
            destruirAvisoVisual();
            temporizadorInactividad = setTimeout(() => { location.reload(); }, TIEMPO_ESPERA);
        } else {
            reiniciarTemporizador();
        }
    });

    window.addEventListener('mousemove', reiniciarTemporizador);
    window.addEventListener('keypress', reiniciarTemporizador);
    window.addEventListener('click', reiniciarTemporizador);
    window.addEventListener('scroll', reiniciarTemporizador);


    // ==========================================
    // --- FUNCIONES DE DESCARGA E INTERFAZ ---
    // ==========================================

    function mostrarNotificacion(mensaje, esError = false) {
        const toast = document.createElement('div');
        toast.innerText = mensaje;
        toast.style.position = 'fixed';
        toast.style.bottom = '30px';
        toast.style.left = '50%';
        toast.style.transform = 'translateX(-50%)';
        toast.style.backgroundColor = esError ? '#d32f2f' : '#1976d2';
        toast.style.color = 'white';
        toast.style.padding = '12px 24px';
        toast.style.borderRadius = '8px';
        toast.style.zIndex = '9999999';
        toast.style.boxShadow = '0 4px 6px rgba(0,0,0,0.3)';
        toast.style.fontWeight = 'bold';
        toast.style.transition = 'opacity 0.5s';
        toast.style.fontFamily = 'Segoe UI, Helvetica Neue, Arial, sans-serif';

        document.body.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => document.body.removeChild(toast), 500);
        }, 4500);
    }

    async function procesarDescarga(nombreBoton, cantImagenes) {
        mostrarNotificacion(`Iniciando ${nombreBoton}... Buscando ${cantImagenes} imágenes y 1 audio.`);

        // 1. OBTENER IMÁGENES DEL CHAT
        const todasLasImagenes = Array.from(document.querySelectorAll('img[src^="blob:"]')).map(img => img.src);
        const urlsImagenesDeseadas = [...new Set(todasLasImagenes)].slice(-cantImagenes);

        // 2. OBTENER EL AUDIO EXACTO DE LA BÓVEDA
        const urlAudioDeseado = window._bovedaArchivos.ultimoAudioReproducido;
        let audioGuardado = null;

        if (urlAudioDeseado) {
            audioGuardado = window._bovedaArchivos.audios[urlAudioDeseado];
        }

        // --- VALIDACIONES ---
        if (!audioGuardado) {
            mostrarNotificacion("⚠️ No se ha detectado reproducción. Dale 'Play' al audio correcto por un segundo y vuelve a intentar.", true);
            return;
        }

        if (urlsImagenesDeseadas.length < cantImagenes) {
            mostrarNotificacion(`⚠️ Faltan imágenes. Se encontraron ${urlsImagenesDeseadas.length} de ${cantImagenes}. Haz scroll para que carguen en pantalla.`, true);
            return;
        }

        let descargasExitosas = 0;
        const timestamp = new Date().getTime();

        // 3. DESCARGAR IMÁGENES
        for (let i = 0; i < urlsImagenesDeseadas.length; i++) {
            const urlImagen = urlsImagenesDeseadas[i];

            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = urlImagen;
            a.download = `WP_IMG_${timestamp}_${i+1}.jpg`;
            document.body.appendChild(a);
            a.click();
            setTimeout(() => { document.body.removeChild(a); }, 1000);

            descargasExitosas++;
            await new Promise(r => setTimeout(r, 800)); // Pausa vital para evitar bloqueos del navegador
        }

        // 4. DESCARGAR EL AUDIO EXACTO
        const urlAudioTemporal = window.URL.createObjectURL(audioGuardado);
        const aAudio = document.createElement('a');
        aAudio.style.display = 'none';
        aAudio.href = urlAudioTemporal;
        aAudio.download = `WP_AUDIO_${timestamp}.ogg`;
        document.body.appendChild(aAudio);
        aAudio.click();
        setTimeout(() => { document.body.removeChild(aAudio); }, 1000);
        descargasExitosas++;

        mostrarNotificacion(`¡${nombreBoton} completado! Se descargaron ${descargasExitosas} archivos.`);
    }

    function agregarInterfaz() {
        if (document.getElementById('panel-auditor-wa')) return;

        const lineaRoja = document.createElement('div');
        lineaRoja.style.position = 'fixed';
        lineaRoja.style.top = '0';
        lineaRoja.style.left = '0';
        lineaRoja.style.width = '100%';
        lineaRoja.style.height = '5px';
        lineaRoja.style.backgroundColor = 'red';
        lineaRoja.style.zIndex = '999999';

        const panelBotones = document.createElement('div');
        panelBotones.id = 'panel-auditor-wa';
        panelBotones.style.position = 'fixed';
        panelBotones.style.top = '15px';
        panelBotones.style.right = '20px';
        panelBotones.style.zIndex = '999999';
        panelBotones.style.display = 'flex';
        panelBotones.style.gap = '10px';

        function crearBoton(texto, colorFondo, cantImagenes) {
            const btn = document.createElement('button');
            btn.innerText = texto;
            btn.style.backgroundColor = colorFondo;
            btn.style.color = 'white';
            btn.style.border = 'none';
            btn.style.padding = '10px 15px';
            btn.style.borderRadius = '8px';
            btn.style.cursor = 'pointer';
            btn.style.fontWeight = 'bold';
            btn.addEventListener('click', () => procesarDescarga(texto, cantImagenes));
            return btn;
        }

        panelBotones.appendChild(crearBoton('Recuadro', '#e65100', 2));
        panelBotones.appendChild(crearBoton('Descargar', 'purple', 3));
        panelBotones.appendChild(crearBoton('WINBACK', '#1565c0', 4));

        document.body.appendChild(lineaRoja);
        document.body.appendChild(panelBotones);

        reiniciarTemporizador();
    }

    const observer = new MutationObserver((mutations, obs) => {
        if (document.body) {
            agregarInterfaz();
            obs.disconnect();
        }
    });

    observer.observe(document.documentElement, { childList: true, subtree: true });

})();