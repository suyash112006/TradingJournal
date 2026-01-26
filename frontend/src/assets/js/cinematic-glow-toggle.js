/**
 * Cinematic Glow Toggle Component
 * A stylized on/off switch with smooth animations
 * Works with plain HTML/CSS/JS - no React required
 */

class CinematicSwitch {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.isOn = false;
        this.init();
    }

    init() {
        this.container.innerHTML = this.getHTML();
        this.track = this.container.querySelector('[data-track]');
        this.thumb = this.container.querySelector('[data-thumb]');
        this.offLabel = this.container.querySelector('[data-off-label]');
        this.onLabel = this.container.querySelector('[data-on-label]');
        this.switchContainer = this.container.querySelector('[data-switch-container]');

        this.switchContainer.addEventListener('click', () => this.toggle());
    }

    getHTML() {
        return `
            <div data-switch-container class="flex items-center gap-4 p-4 rounded-2xl bg-zinc-900/50 border border-zinc-800 backdrop-blur-sm shadow-xl cursor-pointer transition-all duration-300 hover:border-zinc-700">
                <!-- OFF Label -->
                <span data-off-label class="text-xs font-bold tracking-wider transition-colors duration-300 text-zinc-400">
                    OFF
                </span>

                <!-- Switch Track -->
                <div data-track class="relative w-16 h-8 rounded-full shadow-inner bg-zinc-800 transition-all duration-300">
                    <!-- Switch Thumb -->
                    <div data-thumb class="absolute top-1 left-1 w-6 h-6 rounded-full border border-white/10 shadow-md bg-zinc-600 transition-all duration-300">
                        <!-- Thumb Highlight (Gloss) -->
                        <div class="absolute top-1 left-1.5 w-2 h-1 bg-white/30 rounded-full blur-[1px]"></div>
                    </div>
                </div>

                <!-- ON Label -->
                <span data-on-label class="text-xs font-bold tracking-wider transition-colors duration-300 text-zinc-700">
                    ON
                </span>
            </div>
        `;
    }

    toggle() {
        this.isOn = !this.isOn;
        this.updateUI();
        this.dispatchEvent();
    }

    updateUI() {
        const duration = '300ms';
        
        // Update track color
        if (this.isOn) {
            this.track.style.backgroundColor = '#064e3b'; // Emerald-900
            this.thumb.style.transform = 'translateX(32px)';
            this.thumb.style.backgroundColor = '#34d399'; // Emerald-400
            this.offLabel.style.color = '#18181b';
            this.onLabel.style.color = '#34d399';
            this.onLabel.style.filter = 'drop-shadow(0 0 8px rgba(52, 211, 153, 0.5))';
        } else {
            this.track.style.backgroundColor = '#27272a'; // Zinc-800
            this.thumb.style.transform = 'translateX(0)';
            this.thumb.style.backgroundColor = '#52525b'; // Zinc-600
            this.offLabel.style.color = '#a1a1aa';
            this.onLabel.style.color = '#3f3f46';
            this.onLabel.style.filter = 'none';
        }
    }

    dispatchEvent() {
        const event = new CustomEvent('toggle', {
            detail: { isOn: this.isOn }
        });
        this.container.dispatchEvent(event);
    }

    getState() {
        return this.isOn;
    }

    setState(value) {
        if (this.isOn !== value) {
            this.toggle();
        }
    }
}

// Auto-initialize all switches with data-cinematic-switch attribute
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-cinematic-switch]').forEach((el) => {
        const id = el.id || `switch-${Math.random().toString(36).substr(2, 9)}`;
        el.id = id;
        const switchInstance = new CinematicSwitch(id);
        el.dataset.switchInstance = switchInstance;
    });
});
