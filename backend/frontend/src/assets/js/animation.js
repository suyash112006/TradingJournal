/**
 * PORTABLE LOGIN THEME ANIMATIONS
 * Handles:
 * 1. Floating Background Particles
 * 2. Twinkling Background Stars
 * 3. 3D Tilt Effect on Login Card
 */

document.addEventListener('DOMContentLoaded', () => {
    initThemeParticles();
    initThemeStars();
    // initThemeTilt(); // Disabled as per user request
});

// 1. Initialize Floating Particles
function initThemeParticles() {
    const container = document.getElementById('theme-particles');
    // If user forgot to add the container, create it automatically
    if (!container) {
        console.warn('Particle container #theme-particles not found.');
        return;
    }

    const particleCount = 120; // Increased for "full" background

    for (let i = 0; i < particleCount; i++) {
        const p = document.createElement('div');
        p.className = 'bg-particle';

        // Randomize
        const left = Math.random() * 100;
        const size = 3 + Math.random() * 3; // 3px to 6px (Medium)
        const delay = Math.random() * -20; // Negative delay to start mid-animation
        const duration = 15 + Math.random() * 15; // Slower, more atmospheric float

        p.style.left = `${left}%`;
        p.style.bottom = '-10px';
        p.style.width = `${size}px`;
        p.style.height = `${size}px`;
        p.style.animationDuration = `${duration}s`;
        p.style.animationDelay = `${delay}s`;

        container.appendChild(p);
    }
}

// 2. Initialize Twinkling Stars
function initThemeStars() {
    const container = document.getElementById('theme-stars');
    if (!container) return;

    for (let i = 0; i < 40; i++) {
        const star = document.createElement('div');
        star.className = 'bg-star';

        const top = Math.random() * 100;
        const left = Math.random() * 100;
        const size = 1 + Math.random() * 2;
        const delay = Math.random() * 5;

        star.style.top = `${top}%`;
        star.style.left = `${left}%`;
        star.style.width = `${size}px`;
        star.style.height = `${size}px`;
        star.style.animationDelay = `${delay}s`;

        container.appendChild(star);
    }
}

// 3. 3D Tilt Effect for Login Card
function initThemeTilt() {
    const card = document.querySelector('.glass-panel');
    const wrapper = document.querySelector('.login-card-3d');

    if (!card || !wrapper) return;

    wrapper.addEventListener('mousemove', (e) => {
        const rect = wrapper.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        // Calculate rotation (max 5 degrees)
        const xPct = (x / rect.width) - 0.5;
        const yPct = (y / rect.height) - 0.5;

        const rotY = xPct * 10;  // Rotate Y based on mouse X
        const rotX = -yPct * 10; // Rotate X based on mouse Y

        card.style.transform = `perspective(1000px) rotateX(${rotX}deg) rotateY(${rotY}deg) scale(1.02)`;
    });

    wrapper.addEventListener('mouseleave', () => {
        // Reset position
        card.style.transform = `perspective(1000px) rotateX(0deg) rotateY(0deg) scale(1)`;
    });
}

/**
 * Universal Password Toggle logic
 * @param {string} inputId - ID of the input field
 * @param {HTMLElement} iconElement - The icon clicked
 */
function togglePassword(inputId, iconElement) {
    const input = document.getElementById(inputId);
    if (!input) return;

    if (input.type === 'password') {
        input.type = 'text';
        iconElement.setAttribute('data-lucide', 'eye-off');
    } else {
        input.type = 'password';
        iconElement.setAttribute('data-lucide', 'eye');
    }

    // Re-render icons if using Lucide
    if (window.lucide) {
        window.lucide.createIcons();
    }
}
