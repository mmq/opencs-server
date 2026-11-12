"""The account pages' forms. They hold a value to its shape only; the game server's rules decide
the rest, and its refusal is shown as the form's error."""

from __future__ import annotations

from typing import Any

from django import forms

from opencs_web import accounts


class ChallengeForm(forms.Form):
    """A form a script is unlikely to send back as a person would (``accounts``): a field people
    do not see, which must stay empty, and the signed time it was shown."""

    website = forms.CharField(required=False, label="Leave this empty")
    stamp = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["stamp"].initial = accounts.form_stamp()
        self.fields["website"].widget.attrs.update({"tabindex": "-1", "autocomplete": "off"})

    def renew(self) -> None:
        """Give a form shown again a new time, so one refused for its time can be sent back."""
        if self.is_bound:
            self.data = {**dict(self.data.items()), "stamp": accounts.form_stamp()}

    @property
    def caught(self) -> bool:
        """The hidden field was filled in: answered as if it worked, and nothing is sent."""
        return bool(self.data.get("website"))

    def clean_stamp(self) -> str:
        stamp: str = self.cleaned_data["stamp"]
        if (problem := accounts.stamp_problem(stamp)) is not None:
            raise forms.ValidationError(problem)
        return stamp


def _email_field() -> forms.EmailField:
    return forms.EmailField(label="Email address", max_length=254)


class NameFields(forms.Form):
    name = forms.CharField(
        label="Account name",
        max_length=12,
        help_text="What you type to log on: 3 to 12 letters, digits, _ or -.",
    )
    callsign = forms.CharField(
        label="Callsign (optional)",
        max_length=12,
        required=False,
        help_text=(
            "What other pilots see: 3 to 12 letters, digits and punctuation. "
            "Leave it empty to choose one at your first logon."
        ),
    )

    def clean_name(self) -> str:
        name: str = self.cleaned_data["name"].strip().upper()
        if (problem := accounts.name_problem(name)) is not None:
            raise forms.ValidationError(problem)
        return name

    def clean_callsign(self) -> str:
        callsign: str = self.cleaned_data["callsign"].strip()
        if callsign and (problem := accounts.callsign_problem(callsign)) is not None:
            raise forms.ValidationError(problem)
        return callsign


class RegisterForm(NameFields, ChallengeForm):
    email = _email_field()

    field_order = ("name", "callsign", "email", "website", "stamp")

    def clean_email(self) -> str:
        return accounts.normal_email(self.cleaned_data["email"])


class LoginForm(ChallengeForm):
    email = _email_field()

    field_order = ("email", "website", "stamp")

    def clean_email(self) -> str:
        return accounts.normal_email(self.cleaned_data["email"])


class PasswordField(forms.CharField):
    def __init__(self) -> None:
        super().__init__(
            label="Password (optional)",
            max_length=40,
            required=False,
            strip=False,
            widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
            help_text=(
                "6 to 12 capitals, digits and punctuation, as the game's password entry types "
                "them. Leave it empty for one made up for you, which is safer: the game sends "
                "passwords unencrypted, so use one you use nowhere else."
            ),
        )


def _clean_password(form: forms.Form) -> str:
    password: str = form.cleaned_data.get("password", "")
    if password and (problem := accounts.password_problem(password)) is not None:
        raise forms.ValidationError(problem)
    return password


class ConfirmForm(NameFields):
    """The last step of a registration: the name and callsign asked for, which may be changed if
    the server refuses them, and a password if players may choose."""

    def __init__(self, *args: Any, chosen: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if chosen:
            self.fields["password"] = PasswordField()

    def clean_password(self) -> str:
        return _clean_password(self)


class CallsignForm(forms.Form):
    callsign = forms.CharField(label="New callsign", max_length=12)

    def clean_callsign(self) -> str:
        callsign: str = self.cleaned_data["callsign"].strip()
        if (problem := accounts.callsign_problem(callsign)) is not None:
            raise forms.ValidationError(problem)
        return callsign


class NewPasswordForm(forms.Form):
    def __init__(self, *args: Any, chosen: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if chosen:
            self.fields["password"] = PasswordField()

    def clean_password(self) -> str:
        return _clean_password(self)


class DeleteForm(forms.Form):
    FORGET = "forget"
    GAME = "game"

    what = forms.ChoiceField(
        choices=[
            (FORGET, "Forget my email address here; keep my game account"),
            (GAME, "Delete my game account too: its name, callsign, password and progress"),
        ],
        widget=forms.RadioSelect,
        label="What to delete",
    )
    confirm = forms.CharField(label="Type your account name to confirm", max_length=12)

    def __init__(self, *args: Any, game_name: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._game_name = game_name

    def clean_confirm(self) -> str:
        confirm: str = self.cleaned_data["confirm"].strip().upper()
        if confirm != self._game_name:
            raise forms.ValidationError("That is not your account name.")
        return confirm
