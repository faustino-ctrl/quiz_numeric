document.addEventListener("DOMContentLoaded", function () {
    const linksLogout = document.querySelectorAll(".confirmar-logout");

    linksLogout.forEach(function (link) {
        link.addEventListener("click", function (evento) {
            const confirmar = confirm("Deseja realmente sair da conta?");
            if (!confirmar) {
                evento.preventDefault();
            }
        });
    });
});
