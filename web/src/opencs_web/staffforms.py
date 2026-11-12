"""The admin pages' forms (``staffviews``). Each action's form holds its arguments to the shape the
service channel takes; the game server decides the rest and its reply is shown."""

from __future__ import annotations

from django import forms

from opencs import policy


def _one_word(value: str, what: str) -> str:
    """A name or a callsign: one word of printable text (the server says whether it exists)."""
    value = value.strip()
    if not value or not all(" " < ch <= "~" for ch in value):
        raise forms.ValidationError(f"One word. {what}")
    return value


class AdminLoginForm(forms.Form):
    name = forms.CharField(label="Admin name", max_length=150)
    password = forms.CharField(
        label="Password",
        max_length=200,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    code = forms.CharField(
        label="Code",
        max_length=20,
        required=False,  # none is a wrong one, and said so alike
        help_text="Six digits from your authenticator app, or one of your recovery codes.",
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


class CodeForm(forms.Form):
    """A fresh code, asked for before a destructive action when the last was a while ago."""

    code = forms.CharField(
        label="Code",
        max_length=20,
        required=False,
        help_text="Six digits from your authenticator app, or a recovery code.",
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


class NoArguments(forms.Form):
    pass


class KickForm(forms.Form):
    callsign = forms.CharField(label="Callsign", max_length=policy.MAX_ACCOUNT_NAME_LENGTH)
    reason = forms.CharField(
        label="Reason (optional, shown to the pilot)", max_length=60, required=False
    )

    def clean_callsign(self) -> str:
        return _one_word(self.cleaned_data["callsign"], "The pilot's callsign, in any case.")

    def clean_reason(self) -> str:
        reason: str = self.cleaned_data["reason"].strip()
        if not all(" " <= ch <= "~" for ch in reason):
            raise forms.ValidationError("Plain letters, digits and punctuation only.")
        return reason


class AnnounceForm(forms.Form):
    text = forms.CharField(label="Announcement, to every player", max_length=120)

    def clean_text(self) -> str:
        text: str = self.cleaned_data["text"].strip()
        if not all(" " <= ch <= "~" for ch in text):
            raise forms.ValidationError("Plain letters, digits and punctuation only.")
        return text


class GuppyForm(forms.Form):
    team = forms.ChoiceField(
        label="Team", choices=[(name, name.title()) for name in policy.TEAM_NAMES]
    )


class AccountNameForm(forms.Form):
    name = forms.CharField(label="Account name", max_length=policy.MAX_ACCOUNT_NAME_LENGTH)

    def clean_name(self) -> str:
        return _one_word(self.cleaned_data["name"], "The name the account logs on with.").upper()


class ResetPasswordForm(AccountNameForm):
    kick = forms.BooleanField(
        label="Kick it as well if it is in the game (a new password does not end a session)",
        required=False,
    )


class ShutdownForm(forms.Form):
    seconds = forms.IntegerField(
        label="Seconds of warning",
        min_value=0,
        max_value=policy.ADMIN_MAX_SHUTDOWN_SECONDS,
        initial=300,
        help_text="Every player is told, and it can be cancelled until then. 0 stops at once.",
    )
