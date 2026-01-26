/**
 * Vanilla JS 3D Tilt Effect
 * Adds a premium 3D interaction to elements.
 */

class VanillaTilt {
    constructor(element, settings = {}) {
        this.element = element;
        this.settings = Object.assign({
            max: 15,            // Max tilt amount
            perspective: 1000,   // Transform perspective
            scale: 1.05,        // Scale on hover
            speed: 400,         // Transition speed
            easing: "cubic-bezier(.03,.98,.52,.99)"
        }, settings);

        this.init();
    }

    init() {
        this.element.addEventListener("mouseenter", this.onMouseEnter.bind(this));
        this.element.addEventListener("mousemove", this.onMouseMove.bind(this));
        this.element.addEventListener("mouseleave", this.onMouseLeave.bind(this));

        // Optimize animation
        this.updateCall = null;
        this.event = null;
    }

    onMouseEnter() {
        this.updateElementPosition();
        this.element.style.willChange = "transform";
        this.setTransition();
    }

    onMouseMove(event) {
        if (this.updateCall !== null) {
            cancelAnimationFrame(this.updateCall);
        }

        this.event = event;
        this.updateCall = requestAnimationFrame(this.update.bind(this));
    }

    onMouseLeave() {
        this.setTransition();

        // Reset transform
        this.element.style.transform = `perspective(${this.settings.perspective}px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)`;
    }

    update() {
        let values = this.getValues();

        this.element.style.transform = `
            perspective(${this.settings.perspective}px) 
            rotateX(${this.element.style.transform === "" ? 0 : values.tiltY}deg) 
            rotateY(${this.element.style.transform === "" ? 0 : values.tiltX}deg) 
            scale3d(${this.settings.scale}, ${this.settings.scale}, ${this.settings.scale})
        `;

        this.updateCall = null;
    }

    getValues() {
        let x = (this.event.clientX - this.left) / this.width;
        let y = (this.event.clientY - this.top) / this.height;

        // Clamp between 0 and 1
        x = Math.min(Math.max(x, 0), 1);
        y = Math.min(Math.max(y, 0), 1);

        let tiltX = (this.settings.max * 2 * x) - this.settings.max;
        let tiltY = -((this.settings.max * 2 * y) - this.settings.max);

        return {
            tiltX: tiltX,
            tiltY: tiltY
        };
    }

    updateElementPosition() {
        let rect = this.element.getBoundingClientRect();
        this.width = rect.width;
        this.height = rect.height;
        this.left = rect.left;
        this.top = rect.top;
    }

    setTransition() {
        clearTimeout(this.transitionTimeout);
        this.element.style.transition = `transform ${this.settings.speed}ms ${this.settings.easing}`;
        this.transitionTimeout = setTimeout(() => {
            this.element.style.transition = "";
        }, this.settings.speed);
    }
}

// Auto-init
document.addEventListener("DOMContentLoaded", () => {
    const elements = document.querySelectorAll(".js-tilt");
    elements.forEach(el => new VanillaTilt(el));
});
