from django.contrib import messages

from django.utils.translation import ugettext as _

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, render

from django.http import HttpResponseRedirect
from django.http import HttpResponseNotFound

from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required

from django.core.mail import send_mail

from django.template.loader import render_to_string

from django.core.urlresolvers import reverse
from django.contrib.sites.models import get_current_site

from django.views.decorators.csrf import csrf_exempt

from django.conf import settings

from apps.account.models import Invitation, UserProfile
from apps.account.forms import FollowForm

from apps.question.models import Question
from apps.question.views import build_answer_queryset
from apps.follow.models import UserFollow

from datetime import datetime, timedelta

from apps.account.forms import RegisterForm
from apps.account.forms import UpdateProfileForm
from apps.account.forms import EmailCandidateForm

from apps.account.models import EmailCandidate
from apps.account.signals import follower_count_changed

from redis_cache import get_redis_connection
from tastypie.models import ApiKey

from utils import render_to_json

redis = get_redis_connection('default')


def profile(request, username=None, action=None, get_filter='public'):

    user = get_object_or_404(User, username=username) if username \
        else request.user

    if get_filter != 'public' and not request.user == user:
        return HttpResponseNotFound()

    user_is_blocked_me, user_is_blocked_by_me,\
        i_am_follower_of_user, have_pending_follow_request \
        = False, False, False, False

    if request.user.is_authenticated():
        user_is_blocked_me = user.is_blocked_by(request.user)
        user_is_blocked_by_me = user.is_blocked_by(request.user)
        i_am_follower_of_user = request.user.is_following(user)
        have_pending_follow_request = \
            request.user.has_pending_follow_request(user)

    ctx = {'profile_user': user,
           'user_is_blocked_by_me': user_is_blocked_by_me,
           'user_is_blocked_me': user_is_blocked_me,
           'have_pending_follow_request': have_pending_follow_request,
           'i_am_follower_of_user': i_am_follower_of_user}

    # If there are not blocks, fill ctx with answers
    if not (user_is_blocked_me or user_is_blocked_by_me):
        ctx['answers'] = build_answer_queryset(
            request, get_from='user', get_filter=get_filter, user=user)

    if action:
        ctx['action'] = action
        follow_form = FollowForm(follower=request.user,
                                 target=user,
                                 action=action)
        if request.POST:
            follow_form = FollowForm(request.POST,
                                     follower=request.user,
                                     target=user,
                                     action=action)
            if follow_form.is_valid():
                follow_form.save()
                if action == 'follow':
                    messages.success(
                        request, _('Follow request sent to user'))
                elif action == 'unfollow':
                    messages.success(
                        request, _('You are not a follower anymore'))
                elif action == 'block':
                    messages.success(
                        request, _('You have blocked this user'))
                elif action == 'unblock':
                    messages.success(
                        request, _('You have unblocked this user'))
                return HttpResponseRedirect(user.get_absolute_url())

        ctx['follow_form'] = follow_form

    return render(request, "auth/user_detail.html", ctx)


#TODO: Move this view under follow app
@login_required
def pending_follow_requests(request):
    pending_follow_requests = UserFollow.objects.filter(
        status=0, target=request.user)
    return render(
        request,
        'auth/pending_follow_requests.html',
        {'pending_follow_requests': pending_follow_requests})


#TODO: Move this view under follow app
@csrf_exempt
@login_required
def update_pending_follow_request(request):
    if request.method == 'POST':
        fid = request.POST['id']
        action = request.POST['action']

        follow_request = get_object_or_404(
            UserFollow, id=fid, target=request.user)

        if action == 'accept':
            follow_request.status = 1
            follow_request.save()
            follower_count_changed.send(sender=request.user)
            return render_to_json({'success': True})

        if action == 'decline':
            follow_request.delete()
            return render_to_json({'success': True})
    return render_to_json({'success': False,
                           'message': 'Invalida data'})


@login_required
def update_profile(request):
    profile = get_object_or_404(UserProfile, user=request.user)
    form = UpdateProfileForm(instance=profile)

    if request.POST:

        form = UpdateProfileForm(
            request.POST, request.FILES, instance=profile)

        if form.is_valid():
            profile = form.save(commit=False)
            profile.user = request.user
            profile.save()
            messages.success(request, _('Your profile updated'))
            return HttpResponseRedirect(
                reverse('profile',
                        kwargs={'username': request.user.username}))
    avatar_question = Question.objects.get(id=settings.AVATAR_QUESTION_ID)
    return render(
        request,
        "auth/update_profile.html",
        {'form': form,
         'avatar_question': avatar_question})


def register(request):
    form = RegisterForm(initial={'key': request.GET.get("key", None)})
    if request.POST:
        form = RegisterForm(request.POST)
        if form.is_valid():

            user = form.save()
            user = authenticate(username=form.cleaned_data['username'],
                                password=form.cleaned_data['pass_1'])
            login(request, user)
            messages.success(request, _('Registration complete, wellcome :)'))
            return HttpResponseRedirect("/")
    return render(request, 'auth/register.html', {'form': form})


@login_required
def invitations(request):
    return render(
        request,
        'auth/invitations.html',
        {'site': get_current_site(request),
         'invs': Invitation.objects.filter(
             owner=request.user).order_by("used_by")})


@login_required
def hof(request):
    return render(
        request,
        'auth/hof.html',
        {'profiles': UserProfile.objects.order_by('like_count')[:40]})


@login_required
def remote_key(request):
    remote_key, created = ApiKey.objects.get_or_create(user=request.user)

    if request.POST.get('reset'):
        remote_key.delete()
        remote_key = ApiKey.objects.create(user=request.user)
        messages.success(request, _('Your remote key has been reset'))
        return HttpResponseRedirect(reverse('remote_key'))

    return render(request, 'auth/remote_key.html', {'remote_key': remote_key})


@login_required
def email(request, key=None):
    EmailCandidate.objects.filter(
        created_at__lte=datetime.utcnow() - timedelta(days=6*30)).delete()
    if key:

        try:
            email = EmailCandidate.objects.get(key=key)
        except EmailCandidate.DoesNotExist:
            email = None

        if email:
            user = email.owner
            user.email = email.email
            user.save()
            messages.success(request, _('Your email confirmed :)'))
            return HttpResponseRedirect("/")
        else:
            return render(request, 'auth/email_form.html', {
                'key_wrong': True})
    else:
        if request.POST:
            form = EmailCandidateForm(request.POST)
            if form.is_valid():
                candidate = form.save(commit=False)
                candidate.owner = request.user
                candidate.save()

                email_ctx = render_to_string('emails/confirmation_body.txt', {
                    'domain': get_current_site(request),
                    'candidate': candidate})

                send_mail(_('E-mail confirmation'),
                          email_ctx,
                          settings.EMAIL_FROM,
                          [candidate.email],
                          fail_silently=False)

                return render(request, 'auth/email_form.html')
            else:
                return render(request, 'auth/email_form.html', {
                    'form': form})
        else:
            return render(request, 'auth/email_form.html', {
                'form': EmailCandidateForm})