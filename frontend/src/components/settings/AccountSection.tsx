import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import toast from 'react-hot-toast';
import { ArrowRightStartOnRectangleIcon } from '@heroicons/react/24/outline';
import { useNavigate } from 'react-router-dom';
import client, { apiErrorMessage } from '../../api/client';
import { getUser, setUserOrg, clearAuth } from '../../utils/auth';
import { resetPreferencesBoot } from '../../utils/preferences';
import { useQueryCache } from '../../stores/queryCache';
import { Button } from '../ui/Button';
import { Input } from '../ui/Input';
import { SectionHead } from '../common/SectionHead';

/**
 * Account — the owner's login identity. Name (editable), email (set at
 * first-boot bootstrap, read-only), change-password, and sign-out-everywhere.
 * Backed by /auth/me, /auth/change-password, /auth/logout-all.
 */
export const AccountSection: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [user, setUser] = useState(getUser());
  const [name, setName] = useState(user?.name || '');
  const [email, setEmail] = useState(user?.email || '');
  const [savingProfile, setSavingProfile] = useState(false);

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [changingPassword, setChangingPassword] = useState(false);

  useEffect(() => {
    client
      .get('/auth/me')
      .then((r) => {
        const u = r.data;
        if (u) {
          setUser(u);
          setName(u.name || '');
          setEmail(u.email || '');
          setUserOrg(u, null);
        }
      })
      .catch(() => {
        /* fall back to the cached user */
      });
  }, []);

  const handleSaveProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingProfile(true);
    try {
      const r = await client.patch('/auth/me', { name });
      const u = r.data;
      if (u) {
        setUser(u);
        setUserOrg(u, null);
      }
      toast.success(t('Profile updated'));
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Failed to update profile')));
    } finally {
      setSavingProfile(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      toast.error(t('New passwords do not match'));
      return;
    }
    setChangingPassword(true);
    try {
      await client.post('/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      });
      toast.success(t('Password changed'));
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
    } catch (err) {
      toast.error(apiErrorMessage(err, t('Failed to change password')));
    } finally {
      setChangingPassword(false);
    }
  };

  const handleSignOutEverywhere = async () => {
    if (!window.confirm(t('Sign out of every session on this coordinator?'))) return;
    try {
      await client.post('/auth/logout-all');
    } catch {
      /* best-effort — clear locally regardless */
    }
    // Revoke-everywhere already happened server-side; this tears down local
    // state, including the cached API responses (which performLogout also does
    // for the ordinary sign-out path).
    clearAuth();
    resetPreferencesBoot();
    useQueryCache.getState().clear();
    navigate('/login');
  };

  void user;

  return (
    <div className="space-y-8">
      {/* Identity */}
      <section>
        <SectionHead title={t('Account')} description={t('Your owner login identity.')} />
        <form
          onSubmit={handleSaveProfile}
          className="mt-3 space-y-4 border-t border-border pt-4"
        >
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Name')}</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t('Your name')} />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Email')}</label>
            <Input value={email} disabled />
            <p className="text-xs text-tertiary mt-1">{t('Email is set at first-boot bootstrap.')}</p>
          </div>
          <div className="flex justify-end">
            <Button type="submit" loading={savingProfile}>
              {t('Save')}
            </Button>
          </div>
        </form>
      </section>

      {/* Password */}
      <section>
        <SectionHead title={t('Password')} description={t('Change the owner account password.')} />
        <form
          onSubmit={handleChangePassword}
          className="mt-3 space-y-4 border-t border-border pt-4"
        >
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Current password')}</label>
            <Input
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('New password')}</label>
            <Input
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-[13px] font-medium text-ink mb-1">{t('Confirm new password')}</label>
            <Input
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </div>
          <div className="flex justify-end">
            <Button type="submit" loading={changingPassword} disabled={!currentPassword || !newPassword}>
              {t('Change password')}
            </Button>
          </div>
        </form>
      </section>

      {/* Sessions */}
      <section>
        <SectionHead
          title={t('Sessions')}
          description={t('Sign out of all sessions on this coordinator.')}
        />
        <div className="mt-3 border-t border-border pt-4 flex items-center justify-between gap-4">
          <span className="text-sm text-secondary">
            {t('This ends every active session, including this one.')}
          </span>
          <Button variant="secondary" onClick={handleSignOutEverywhere}>
            <ArrowRightStartOnRectangleIcon className="w-4 h-4" />
            {t('Sign out everywhere')}
          </Button>
        </div>
      </section>
    </div>
  );
};

export default AccountSection;
