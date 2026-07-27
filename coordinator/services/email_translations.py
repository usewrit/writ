"""
Email translations for EN, FR, ES.

Each key is a template identifier. Values are dicts keyed by locale code.
Placeholders use Python str.format() syntax: {name}, {plan_name}, etc.
"""

SUPPORTED_LOCALES = ("en", "fr", "es")
DEFAULT_LOCALE = "en"


def t(key: str, locale: str = "en", **kwargs) -> str:
    data = TRANSLATIONS.get(key, {})
    text = data.get(locale) or data.get("en", key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


# ---------------------------------------------------------------------------
# Shared / layout strings
# ---------------------------------------------------------------------------
TRANSLATIONS = {
    # Footer
    "footer_unsubscribe": {
        "en": "You're receiving this because you have an account on Writ.",
        "fr": "Vous recevez ceci car vous avez un compte sur Writ.",
        "es": "Recibes esto porque tienes una cuenta en Writ.",
    },

    # CAN-SPAM / PECR compliant footer — unsubscribe link + postal address.
    # Only injected when the email is a marketing/non-transactional category
    # (an `unsubscribe_url` is supplied). Transactional mail keeps footer_unsubscribe.
    "footer_unsubscribe_link": {
        "en": "Unsubscribe from marketing emails",
        "fr": "Se desabonner des e-mails marketing",
        "es": "Cancelar la suscripcion a correos de marketing",
    },
    "footer_marketing_reason": {
        "en": "You're receiving this email because you opted in to marketing updates from Writ.",
        "fr": "Vous recevez cet e-mail car vous avez accepte de recevoir les actualites marketing de Writ.",
        "es": "Recibes este correo porque aceptaste recibir novedades de marketing de Writ.",
    },
    # Operator: set your company's real registered postal address (CAN-SPAM footer)
    # before sending any marketing email (CAN-SPAM 5(a)(5) requires a valid physical address).
    "footer_postal_address": {
        "en": "Writ, 123 Example Street, Suite 100, San Francisco, CA 94105, USA",
        "fr": "Writ, 123 Example Street, Suite 100, San Francisco, CA 94105, USA",
        "es": "Writ, 123 Example Street, Suite 100, San Francisco, CA 94105, USA",
    },
    "footer_help": {
        "en": "Need help? Contact our support team.",
        "fr": "Besoin d'aide ? Contactez notre support.",
        "es": "¿Necesitas ayuda? Contacta a nuestro equipo de soporte.",
    },

    # ---------------------------------------------------------------------------
    # AUTH — Welcome
    # ---------------------------------------------------------------------------
    "welcome_subject": {
        "en": "Welcome to Writ, {name}!",
        "fr": "Bienvenue sur Writ, {name} !",
        "es": "¡Bienvenido a Writ, {name}!",
    },
    "welcome_heading": {
        "en": "Welcome to Writ, {name}!",
        "fr": "Bienvenue sur Writ, {name} !",
        "es": "¡Bienvenido a Writ, {name}!",
    },
    "welcome_body": {
        "en": "Your workspace is ready. Here's how to get started:",
        "fr": "Votre espace de travail est prêt. Voici comment commencer :",
        "es": "Tu espacio de trabajo está listo. Así puedes empezar:",
    },
    "welcome_step_1": {
        "en": "Create your first automation workflow",
        "fr": "Créez votre premier flux d'automatisation",
        "es": "Crea tu primer flujo de automatización",
    },
    "welcome_step_2": {
        "en": "Set up a webhook trigger to run it on demand",
        "fr": "Configurez un déclencheur webhook pour l'exécuter à la demande",
        "es": "Configura un webhook para ejecutarlo bajo demanda",
    },
    "welcome_step_3": {
        "en": "Generate an API key for external integrations",
        "fr": "Générez une clé API pour les intégrations externes",
        "es": "Genera una clave API para integraciones externas",
    },
    "welcome_cta": {
        "en": "Go to Dashboard",
        "fr": "Accéder au tableau de bord",
        "es": "Ir al panel",
    },

    # ---------------------------------------------------------------------------
    # AUTH — Email verification
    # ---------------------------------------------------------------------------
    "verify_subject": {
        "en": "Verify your email — Writ",
        "fr": "Vérifiez votre e-mail — Writ",
        "es": "Verifica tu correo — Writ",
    },
    "verify_heading": {
        "en": "Verify your email",
        "fr": "Vérifiez votre e-mail",
        "es": "Verifica tu correo electrónico",
    },
    "verify_body": {
        "en": "Please verify your email address to complete your account setup.",
        "fr": "Veuillez vérifier votre adresse e-mail pour finaliser la création de votre compte.",
        "es": "Verifica tu dirección de correo electrónico para completar la configuración de tu cuenta.",
    },
    "verify_cta": {
        "en": "Verify Email",
        "fr": "Vérifier l'e-mail",
        "es": "Verificar correo",
    },
    "verify_fallback": {
        "en": "Or copy this URL:",
        "fr": "Ou copiez cette URL :",
        "es": "O copia esta URL:",
    },

    # ---------------------------------------------------------------------------
    # AUTH — Password reset
    # ---------------------------------------------------------------------------
    "reset_subject": {
        "en": "Reset your password — Writ",
        "fr": "Réinitialiser votre mot de passe — Writ",
        "es": "Restablecer tu contraseña — Writ",
    },
    "reset_heading": {
        "en": "Reset your password",
        "fr": "Réinitialiser votre mot de passe",
        "es": "Restablecer tu contraseña",
    },
    "reset_body": {
        "en": "We received a request to reset your password. Click the button below to choose a new one.",
        "fr": "Nous avons reçu une demande de réinitialisation de votre mot de passe. Cliquez sur le bouton ci-dessous pour en choisir un nouveau.",
        "es": "Recibimos una solicitud para restablecer tu contraseña. Haz clic en el botón para elegir una nueva.",
    },
    "reset_cta": {
        "en": "Reset Password",
        "fr": "Réinitialiser le mot de passe",
        "es": "Restablecer contraseña",
    },
    "reset_expiry": {
        "en": "This link expires in 1 hour. If you didn't request this, you can safely ignore this email.",
        "fr": "Ce lien expire dans 1 heure. Si vous n'avez pas fait cette demande, vous pouvez ignorer cet e-mail.",
        "es": "Este enlace expira en 1 hora. Si no solicitaste esto, puedes ignorar este correo.",
    },

    # ---------------------------------------------------------------------------
    # AUTH — Password changed confirmation
    # ---------------------------------------------------------------------------
    "password_changed_subject": {
        "en": "Your password has been changed — Writ",
        "fr": "Votre mot de passe a été modifié — Writ",
        "es": "Tu contraseña ha sido cambiada — Writ",
    },
    "password_changed_heading": {
        "en": "Password changed",
        "fr": "Mot de passe modifié",
        "es": "Contraseña cambiada",
    },
    "password_changed_body": {
        "en": "Your password was successfully changed. If you didn't make this change, please reset your password immediately or contact support.",
        "fr": "Votre mot de passe a été modifié avec succès. Si vous n'êtes pas à l'origine de ce changement, veuillez réinitialiser votre mot de passe immédiatement ou contacter le support.",
        "es": "Tu contraseña se cambió correctamente. Si no realizaste este cambio, restablece tu contraseña de inmediato o contacta al soporte.",
    },
    "password_changed_cta": {
        "en": "Reset Password",
        "fr": "Réinitialiser le mot de passe",
        "es": "Restablecer contraseña",
    },

    # ---------------------------------------------------------------------------
    # SUBSCRIPTION — Renewed
    # ---------------------------------------------------------------------------
    "renewed_subject": {
        "en": "Subscription renewed — {plan_name} plan — Writ",
        "fr": "Abonnement renouvelé — forfait {plan_name} — Writ",
        "es": "Suscripción renovada — plan {plan_name} — Writ",
    },
    "renewed_heading": {
        "en": "Payment successful",
        "fr": "Paiement réussi",
        "es": "Pago exitoso",
    },
    "renewed_body": {
        "en": "Hi {name}, your <strong>{plan_name}</strong> subscription has been renewed successfully. Your usage limits and credits have been reset for the new billing period.",
        "fr": "Bonjour {name}, votre abonnement <strong>{plan_name}</strong> a été renouvelé avec succès. Vos limites d'utilisation et crédits ont été réinitialisés pour la nouvelle période de facturation.",
        "es": "Hola {name}, tu suscripción <strong>{plan_name}</strong> se renovó correctamente. Tus límites de uso y créditos se restablecieron para el nuevo período de facturación.",
    },
    "renewed_thanks": {
        "en": "Thank you for using Writ!",
        "fr": "Merci d'utiliser Writ !",
        "es": "¡Gracias por usar Writ!",
    },

    # ---------------------------------------------------------------------------
    # SUBSCRIPTION — Canceled
    # ---------------------------------------------------------------------------
    "canceled_subject": {
        "en": "Your {plan_name} subscription has been canceled — Writ",
        "fr": "Votre abonnement {plan_name} a été annulé — Writ",
        "es": "Tu suscripción {plan_name} ha sido cancelada — Writ",
    },
    "canceled_heading": {
        "en": "Subscription canceled",
        "fr": "Abonnement annulé",
        "es": "Suscripción cancelada",
    },
    "canceled_body": {
        "en": "Hi {name}, your <strong>{plan_name}</strong> subscription has been canceled. Your account has been moved to the Free plan.",
        "fr": "Bonjour {name}, votre abonnement <strong>{plan_name}</strong> a été annulé. Votre compte a été basculé vers le forfait Gratuit.",
        "es": "Hola {name}, tu suscripción <strong>{plan_name}</strong> ha sido cancelada. Tu cuenta se cambió al plan Gratuito.",
    },
    "canceled_what_next_title": {
        "en": "What happens now:",
        "fr": "Que se passe-t-il maintenant :",
        "es": "¿Qué sucede ahora?",
    },
    "canceled_what_next_body": {
        "en": "Your data is preserved, but usage limits have been reduced to the Free plan. You can resubscribe at any time to restore full access.",
        "fr": "Vos données sont conservées, mais les limites d'utilisation sont celles du forfait Gratuit. Vous pouvez vous réabonner à tout moment pour retrouver un accès complet.",
        "es": "Tus datos se conservan, pero los límites de uso se redujeron al plan Gratuito. Puedes volver a suscribirte en cualquier momento para restaurar el acceso completo.",
    },
    "canceled_cta": {
        "en": "Resubscribe",
        "fr": "Se réabonner",
        "es": "Volver a suscribirse",
    },
    "canceled_footer": {
        "en": "We'd love to have you back. If you have feedback, let us know.",
        "fr": "Nous serions ravis de vous revoir. Si vous avez des retours, faites-le nous savoir.",
        "es": "Nos encantaría tenerte de vuelta. Si tienes comentarios, háznoslo saber.",
    },

    # ---------------------------------------------------------------------------
    # SUBSCRIPTION — Expiring
    # ---------------------------------------------------------------------------
    "expiring_subject": {
        "en": "Your {plan_name} plan ends on {end_date} — Writ",
        "fr": "Votre forfait {plan_name} expire le {end_date} — Writ",
        "es": "Tu plan {plan_name} termina el {end_date} — Writ",
    },
    "expiring_heading": {
        "en": "Your subscription is ending soon",
        "fr": "Votre abonnement se termine bientôt",
        "es": "Tu suscripción termina pronto",
    },
    "expiring_body": {
        "en": "Hi {name}, your <strong>{plan_name}</strong> plan is set to cancel on <strong>{end_date}</strong>. After that date, your account will be moved to the Free plan.",
        "fr": "Bonjour {name}, votre forfait <strong>{plan_name}</strong> est programmé pour expirer le <strong>{end_date}</strong>. Après cette date, votre compte sera basculé vers le forfait Gratuit.",
        "es": "Hola {name}, tu plan <strong>{plan_name}</strong> se cancelará el <strong>{end_date}</strong>. Después de esa fecha, tu cuenta se cambiará al plan Gratuito.",
    },
    "expiring_cta": {
        "en": "Keep My Subscription",
        "fr": "Conserver mon abonnement",
        "es": "Mantener mi suscripción",
    },
    "expiring_footer": {
        "en": "If you'd like to continue using Writ, click above to reactivate before the cancellation date.",
        "fr": "Si vous souhaitez continuer à utiliser Writ, cliquez ci-dessus pour réactiver avant la date d'annulation.",
        "es": "Si deseas seguir usando Writ, haz clic arriba para reactivar antes de la fecha de cancelación.",
    },

    # ---------------------------------------------------------------------------
    # SUBSCRIPTION — Upgraded
    # ---------------------------------------------------------------------------
    "upgraded_subject": {
        "en": "You've upgraded to {plan_name} — Writ",
        "fr": "Vous êtes passé au forfait {plan_name} — Writ",
        "es": "Has mejorado al plan {plan_name} — Writ",
    },
    "upgraded_heading": {
        "en": "Plan upgraded!",
        "fr": "Forfait amélioré !",
        "es": "¡Plan mejorado!",
    },
    "upgraded_body": {
        "en": "Hi {name}, you've successfully upgraded to the <strong>{plan_name}</strong> plan. Your new limits and features are active immediately.",
        "fr": "Bonjour {name}, vous êtes passé au forfait <strong>{plan_name}</strong> avec succès. Vos nouvelles limites et fonctionnalités sont actives immédiatement.",
        "es": "Hola {name}, has mejorado al plan <strong>{plan_name}</strong> con éxito. Tus nuevos límites y funciones están activos de inmediato.",
    },
    "upgraded_cta": {
        "en": "Go to Dashboard",
        "fr": "Accéder au tableau de bord",
        "es": "Ir al panel",
    },

    # ---------------------------------------------------------------------------
    # SUBSCRIPTION — Downgraded
    # ---------------------------------------------------------------------------
    "downgraded_subject": {
        "en": "Your plan has been changed to {plan_name} — Writ",
        "fr": "Votre forfait a été changé pour {plan_name} — Writ",
        "es": "Tu plan se cambió a {plan_name} — Writ",
    },
    "downgraded_heading": {
        "en": "Plan changed",
        "fr": "Forfait modifié",
        "es": "Plan cambiado",
    },
    "downgraded_body": {
        "en": "Hi {name}, your plan has been changed to <strong>{plan_name}</strong>. The new limits take effect at your next billing cycle.",
        "fr": "Bonjour {name}, votre forfait a été changé pour <strong>{plan_name}</strong>. Les nouvelles limites prennent effet lors de votre prochain cycle de facturation.",
        "es": "Hola {name}, tu plan se cambió a <strong>{plan_name}</strong>. Los nuevos límites entran en vigor en tu próximo ciclo de facturación.",
    },
    "downgraded_cta": {
        "en": "View Billing",
        "fr": "Voir la facturation",
        "es": "Ver facturación",
    },

    # ---------------------------------------------------------------------------
    # BILLING — Payment failed
    # ---------------------------------------------------------------------------
    "payment_failed_subject": {
        "en": "Action required: Payment failed — Writ",
        "fr": "Action requise : Échec du paiement — Writ",
        "es": "Acción requerida: Pago fallido — Writ",
    },
    "payment_failed_heading": {
        "en": "Payment failed",
        "fr": "Échec du paiement",
        "es": "Pago fallido",
    },
    "payment_failed_body": {
        "en": "Hi {name}, we were unable to process your payment for the <strong>{plan_name}</strong> plan. Please update your payment method to avoid service interruption.",
        "fr": "Bonjour {name}, nous n'avons pas pu traiter votre paiement pour le forfait <strong>{plan_name}</strong>. Veuillez mettre à jour votre moyen de paiement pour éviter une interruption de service.",
        "es": "Hola {name}, no pudimos procesar tu pago para el plan <strong>{plan_name}</strong>. Actualiza tu método de pago para evitar la interrupción del servicio.",
    },
    "payment_failed_warning": {
        "en": "Your account features may be limited until payment is resolved.",
        "fr": "Les fonctionnalités de votre compte peuvent être limitées jusqu'à la résolution du paiement.",
        "es": "Las funciones de tu cuenta pueden estar limitadas hasta que se resuelva el pago.",
    },
    "payment_failed_cta": {
        "en": "Update Payment Method",
        "fr": "Mettre à jour le moyen de paiement",
        "es": "Actualizar método de pago",
    },
    "payment_failed_footer": {
        "en": "If you believe this is an error, please contact support.",
        "fr": "Si vous pensez qu'il s'agit d'une erreur, veuillez contacter le support.",
        "es": "Si crees que esto es un error, contacta al soporte.",
    },

    # ---------------------------------------------------------------------------
    # BILLING — Invoice / receipt
    # ---------------------------------------------------------------------------
    "invoice_subject": {
        "en": "Your Writ invoice — {invoice_number}",
        "fr": "Votre facture Writ — {invoice_number}",
        "es": "Tu factura de Writ — {invoice_number}",
    },
    "invoice_heading": {
        "en": "Invoice",
        "fr": "Facture",
        "es": "Factura",
    },
    "invoice_body": {
        "en": "Hi {name}, here's a summary of your latest payment.",
        "fr": "Bonjour {name}, voici le récapitulatif de votre dernier paiement.",
        "es": "Hola {name}, aquí tienes el resumen de tu último pago.",
    },
    "invoice_plan": {
        "en": "Plan",
        "fr": "Forfait",
        "es": "Plan",
    },
    "invoice_amount": {
        "en": "Amount",
        "fr": "Montant",
        "es": "Monto",
    },
    "invoice_date": {
        "en": "Date",
        "fr": "Date",
        "es": "Fecha",
    },
    "invoice_status": {
        "en": "Status",
        "fr": "Statut",
        "es": "Estado",
    },
    "invoice_paid": {
        "en": "Paid",
        "fr": "Payé",
        "es": "Pagado",
    },
    "invoice_cta": {
        "en": "View Invoice",
        "fr": "Voir la facture",
        "es": "Ver factura",
    },

    # ---------------------------------------------------------------------------
    # TEAM — Invite
    # ---------------------------------------------------------------------------
    "invite_subject": {
        "en": "You've been invited to {org_name} — Writ",
        "fr": "Vous avez été invité à rejoindre {org_name} — Writ",
        "es": "Has sido invitado a {org_name} — Writ",
    },
    "invite_heading": {
        "en": "You've been invited",
        "fr": "Vous avez été invité",
        "es": "Has sido invitado",
    },
    "invite_body": {
        "en": "{inviter_name} invited you to join <strong>{org_name}</strong> on Writ.",
        "fr": "{inviter_name} vous a invité à rejoindre <strong>{org_name}</strong> sur Writ.",
        "es": "{inviter_name} te invitó a unirte a <strong>{org_name}</strong> en Writ.",
    },
    "invite_cta": {
        "en": "Accept Invitation",
        "fr": "Accepter l'invitation",
        "es": "Aceptar invitación",
    },
    "invite_fallback": {
        "en": "Sign in with your existing account to access the workspace.",
        "fr": "Connectez-vous avec votre compte existant pour accéder à l'espace de travail.",
        "es": "Inicia sesión con tu cuenta existente para acceder al espacio de trabajo.",
    },

    # ---------------------------------------------------------------------------
    # TEAM — Member joined (notification to admin)
    # ---------------------------------------------------------------------------
    "member_joined_subject": {
        "en": "{member_name} joined {org_name} — Writ",
        "fr": "{member_name} a rejoint {org_name} — Writ",
        "es": "{member_name} se unió a {org_name} — Writ",
    },
    "member_joined_heading": {
        "en": "New team member",
        "fr": "Nouveau membre",
        "es": "Nuevo miembro del equipo",
    },
    "member_joined_body": {
        "en": "<strong>{member_name}</strong> ({member_email}) has joined your workspace <strong>{org_name}</strong> with the <strong>{role}</strong> role.",
        "fr": "<strong>{member_name}</strong> ({member_email}) a rejoint votre espace de travail <strong>{org_name}</strong> avec le rôle <strong>{role}</strong>.",
        "es": "<strong>{member_name}</strong> ({member_email}) se unió a tu espacio de trabajo <strong>{org_name}</strong> con el rol de <strong>{role}</strong>.",
    },
    "member_joined_cta": {
        "en": "Manage Team",
        "fr": "Gérer l'équipe",
        "es": "Gestionar equipo",
    },

    # ---------------------------------------------------------------------------
    # TEAM — Member removed
    # ---------------------------------------------------------------------------
    "member_removed_subject": {
        "en": "You've been removed from {org_name} — Writ",
        "fr": "Vous avez été retiré de {org_name} — Writ",
        "es": "Has sido eliminado de {org_name} — Writ",
    },
    "member_removed_heading": {
        "en": "Workspace access revoked",
        "fr": "Accès à l'espace de travail révoqué",
        "es": "Acceso al espacio de trabajo revocado",
    },
    "member_removed_body": {
        "en": "You no longer have access to <strong>{org_name}</strong> on Writ. If you believe this is a mistake, please contact the workspace administrator.",
        "fr": "Vous n'avez plus accès à <strong>{org_name}</strong> sur Writ. Si vous pensez qu'il s'agit d'une erreur, veuillez contacter l'administrateur de l'espace de travail.",
        "es": "Ya no tienes acceso a <strong>{org_name}</strong> en Writ. Si crees que esto es un error, contacta al administrador del espacio de trabajo.",
    },

    # ---------------------------------------------------------------------------
    # TEAM — Role changed
    # ---------------------------------------------------------------------------
    "role_changed_subject": {
        "en": "Your role in {org_name} has changed — Writ",
        "fr": "Votre rôle dans {org_name} a changé — Writ",
        "es": "Tu rol en {org_name} ha cambiado — Writ",
    },
    "role_changed_heading": {
        "en": "Role updated",
        "fr": "Rôle mis à jour",
        "es": "Rol actualizado",
    },
    "role_changed_body": {
        "en": "Your role in <strong>{org_name}</strong> has been changed from <strong>{old_role}</strong> to <strong>{new_role}</strong>.",
        "fr": "Votre rôle dans <strong>{org_name}</strong> a été changé de <strong>{old_role}</strong> à <strong>{new_role}</strong>.",
        "es": "Tu rol en <strong>{org_name}</strong> se cambió de <strong>{old_role}</strong> a <strong>{new_role}</strong>.",
    },
    "role_changed_cta": {
        "en": "Go to Workspace",
        "fr": "Accéder à l'espace de travail",
        "es": "Ir al espacio de trabajo",
    },

    # ---------------------------------------------------------------------------
    # ACCOUNT — Deleted
    # ---------------------------------------------------------------------------
    "account_deleted_subject": {
        "en": "Your Writ account has been deleted",
        "fr": "Votre compte Writ a été supprimé",
        "es": "Tu cuenta de Writ ha sido eliminada",
    },
    "account_deleted_heading": {
        "en": "Account deleted",
        "fr": "Compte supprimé",
        "es": "Cuenta eliminada",
    },
    "account_deleted_body": {
        "en": "Your Writ account and all associated data have been permanently deleted. If you didn't request this, please contact support immediately.",
        "fr": "Votre compte Writ et toutes les données associées ont été supprimés définitivement. Si vous n'avez pas fait cette demande, veuillez contacter le support immédiatement.",
        "es": "Tu cuenta de Writ y todos los datos asociados se eliminaron permanentemente. Si no solicitaste esto, contacta al soporte de inmediato.",
    },

    # ---------------------------------------------------------------------------
    # ACCOUNT — Security alert (new login)
    # ---------------------------------------------------------------------------
    "security_alert_subject": {
        "en": "New sign-in to your Writ account",
        "fr": "Nouvelle connexion à votre compte Writ",
        "es": "Nuevo inicio de sesión en tu cuenta de Writ",
    },
    "security_alert_heading": {
        "en": "New sign-in detected",
        "fr": "Nouvelle connexion détectée",
        "es": "Nuevo inicio de sesión detectado",
    },
    "security_alert_body": {
        "en": "We noticed a new sign-in to your Writ account.",
        "fr": "Nous avons détecté une nouvelle connexion à votre compte Writ.",
        "es": "Detectamos un nuevo inicio de sesión en tu cuenta de Writ.",
    },
    "security_alert_device": {
        "en": "Device",
        "fr": "Appareil",
        "es": "Dispositivo",
    },
    "security_alert_location": {
        "en": "Location",
        "fr": "Localisation",
        "es": "Ubicación",
    },
    "security_alert_time": {
        "en": "Time",
        "fr": "Heure",
        "es": "Hora",
    },
    "security_alert_warning": {
        "en": "If this wasn't you, please reset your password immediately.",
        "fr": "Si ce n'était pas vous, veuillez réinitialiser votre mot de passe immédiatement.",
        "es": "Si no fuiste tú, restablece tu contraseña de inmediato.",
    },
    "security_alert_cta": {
        "en": "Review Account Activity",
        "fr": "Vérifier l'activité du compte",
        "es": "Revisar actividad de la cuenta",
    },

    # ---------------------------------------------------------------------------
    # ACCOUNT — Usage limit warning
    # ---------------------------------------------------------------------------
    "usage_warning_subject": {
        "en": "You've reached {percent}% of your {resource} limit — Writ",
        "fr": "Vous avez atteint {percent} % de votre limite de {resource} — Writ",
        "es": "Has alcanzado el {percent}% de tu límite de {resource} — Writ",
    },
    "usage_warning_heading": {
        "en": "Usage limit warning",
        "fr": "Avertissement de limite d'utilisation",
        "es": "Advertencia de límite de uso",
    },
    "usage_warning_body": {
        "en": "Hi {name}, you've used <strong>{percent}%</strong> of your <strong>{resource}</strong> allocation on the <strong>{plan_name}</strong> plan ({used} of {total}).",
        "fr": "Bonjour {name}, vous avez utilisé <strong>{percent} %</strong> de votre allocation de <strong>{resource}</strong> sur le forfait <strong>{plan_name}</strong> ({used} sur {total}).",
        "es": "Hola {name}, has usado el <strong>{percent}%</strong> de tu asignación de <strong>{resource}</strong> en el plan <strong>{plan_name}</strong> ({used} de {total}).",
    },
    "usage_warning_cta": {
        "en": "Upgrade Plan",
        "fr": "Améliorer le forfait",
        "es": "Mejorar plan",
    },

    # ---------------------------------------------------------------------------
    # SUPPORT — Ticket created
    # ---------------------------------------------------------------------------
    "ticket_created_subject": {
        "en": "Support ticket #{ticket_id} created — Writ",
        "fr": "Ticket de support #{ticket_id} créé — Writ",
        "es": "Ticket de soporte #{ticket_id} creado — Writ",
    },
    "ticket_created_heading": {
        "en": "Ticket created",
        "fr": "Ticket créé",
        "es": "Ticket creado",
    },
    "ticket_created_body": {
        "en": "We've received your support request. A member of our team will respond shortly.",
        "fr": "Nous avons bien reçu votre demande de support. Un membre de notre équipe vous répondra rapidement.",
        "es": "Hemos recibido tu solicitud de soporte. Un miembro de nuestro equipo te responderá pronto.",
    },
    "ticket_created_ref": {
        "en": "Reference",
        "fr": "Référence",
        "es": "Referencia",
    },
    "ticket_created_subject_label": {
        "en": "Subject",
        "fr": "Objet",
        "es": "Asunto",
    },
    "ticket_created_cta": {
        "en": "View Ticket",
        "fr": "Voir le ticket",
        "es": "Ver ticket",
    },

    # ---------------------------------------------------------------------------
    # SUPPORT — Ticket reply
    # ---------------------------------------------------------------------------
    "ticket_reply_subject": {
        "en": "New reply on ticket #{ticket_id} — Writ",
        "fr": "Nouvelle réponse sur le ticket #{ticket_id} — Writ",
        "es": "Nueva respuesta en el ticket #{ticket_id} — Writ",
    },
    "ticket_reply_heading": {
        "en": "New reply on your ticket",
        "fr": "Nouvelle réponse à votre ticket",
        "es": "Nueva respuesta en tu ticket",
    },
    "ticket_reply_body": {
        "en": "There's a new reply on your support ticket <strong>#{ticket_id}</strong>.",
        "fr": "Il y a une nouvelle réponse sur votre ticket de support <strong>#{ticket_id}</strong>.",
        "es": "Hay una nueva respuesta en tu ticket de soporte <strong>#{ticket_id}</strong>.",
    },
    "ticket_reply_cta": {
        "en": "View Conversation",
        "fr": "Voir la conversation",
        "es": "Ver conversación",
    },

    # ---------------------------------------------------------------------------
    # QUOTE — Quote sent
    # ---------------------------------------------------------------------------
    "quote_sent_subject": {
        "en": "Your custom quote from Writ — {quote_ref}",
        "fr": "Votre devis personnalisé de Writ — {quote_ref}",
        "es": "Tu cotización personalizada de Writ — {quote_ref}",
    },
    "quote_sent_heading": {
        "en": "Your custom quote",
        "fr": "Votre devis personnalisé",
        "es": "Tu cotización personalizada",
    },
    "quote_sent_body": {
        "en": "Hi {name}, here's the custom quote you requested. This quote is valid until <strong>{valid_until}</strong>.",
        "fr": "Bonjour {name}, voici le devis personnalisé que vous avez demandé. Ce devis est valable jusqu'au <strong>{valid_until}</strong>.",
        "es": "Hola {name}, aquí tienes la cotización personalizada que solicitaste. Esta cotización es válida hasta el <strong>{valid_until}</strong>.",
    },
    "quote_sent_cta": {
        "en": "View Quote",
        "fr": "Voir le devis",
        "es": "Ver cotización",
    },

    # ---------------------------------------------------------------------------
    # QUOTE — Quote accepted
    # ---------------------------------------------------------------------------
    "quote_accepted_subject": {
        "en": "Quote {quote_ref} accepted — Writ",
        "fr": "Devis {quote_ref} accepté — Writ",
        "es": "Cotización {quote_ref} aceptada — Writ",
    },
    "quote_accepted_heading": {
        "en": "Quote accepted",
        "fr": "Devis accepté",
        "es": "Cotización aceptada",
    },
    "quote_accepted_body": {
        "en": "Hi {name}, your quote <strong>{quote_ref}</strong> has been accepted. We'll start setting up your custom plan shortly.",
        "fr": "Bonjour {name}, votre devis <strong>{quote_ref}</strong> a été accepté. Nous commencerons à configurer votre forfait personnalisé sous peu.",
        "es": "Hola {name}, tu cotización <strong>{quote_ref}</strong> ha sido aceptada. Comenzaremos a configurar tu plan personalizado en breve.",
    },

    # ---------------------------------------------------------------------------
    # QUOTE — Quote expired
    # ---------------------------------------------------------------------------
    "quote_expired_subject": {
        "en": "Your quote {quote_ref} has expired — Writ",
        "fr": "Votre devis {quote_ref} a expiré — Writ",
        "es": "Tu cotización {quote_ref} ha expirado — Writ",
    },
    "quote_expired_heading": {
        "en": "Quote expired",
        "fr": "Devis expiré",
        "es": "Cotización expirada",
    },
    "quote_expired_body": {
        "en": "Hi {name}, your quote <strong>{quote_ref}</strong> has expired. Contact us if you'd like a new one.",
        "fr": "Bonjour {name}, votre devis <strong>{quote_ref}</strong> a expiré. Contactez-nous si vous en souhaitez un nouveau.",
        "es": "Hola {name}, tu cotización <strong>{quote_ref}</strong> ha expirado. Contáctanos si deseas una nueva.",
    },
    "quote_expired_cta": {
        "en": "Request New Quote",
        "fr": "Demander un nouveau devis",
        "es": "Solicitar nueva cotización",
    },
}
