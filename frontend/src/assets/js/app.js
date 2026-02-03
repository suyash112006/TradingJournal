// Main App Logic

document.addEventListener('DOMContentLoaded', () => {
    console.log('Trading Journal App Initialized');

    // Here we can handle simple navigation highlighting or mock data loading
    const currentPath = window.location.pathname;
    const navItems = document.querySelectorAll('.nav-item');

    navItems.forEach(item => {
        const href = item.getAttribute('href');
        if (currentPath.includes(href)) {
            item.classList.add('active');
        }
    });
});


// Image Modal Logic
function openFullscreen(src, imgElement) {
    // If it's a broken image (placeholder), open original URL in new tab
    if (imgElement && imgElement.classList.contains('broken-link')) {
        window.open(imgElement.getAttribute('data-original-src'), '_blank');
        return;
    }

    // Create modal elements
    const modal = document.createElement('div');
    modal.style.position = 'fixed';
    modal.style.top = '0';
    modal.style.left = '0';
    modal.style.width = '100%';
    modal.style.height = '100%';
    modal.style.background = 'rgba(0,0,0,0.9)';
    modal.style.zIndex = '9999';
    modal.style.display = 'flex';
    modal.style.alignItems = 'center';
    modal.style.justifyContent = 'center';
    modal.style.cursor = 'zoom-out';

    const img = document.createElement('img');
    img.src = src;
    img.style.maxWidth = '90%';
    img.style.maxHeight = '90%';
    img.style.boxShadow = '0 0 20px rgba(0,0,0,0.5)';
    img.style.borderRadius = '8px';

    // Close on click
    modal.onclick = () => {
        document.body.removeChild(modal);
    };

    modal.appendChild(img);
    document.body.appendChild(modal);
}
