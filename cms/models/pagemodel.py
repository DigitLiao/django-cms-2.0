from os.path import join
from datetime import datetime
from django.db import models
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from django.core.urlresolvers import reverse
from django.contrib.sites.models import Site
from django.shortcuts import get_object_or_404
from publisher import Publisher, Mptt
from publisher.errors import MpttCantPublish
from cms.utils.urlutils import urljoin
from cms import settings
from cms.models.managers import PageManager, PagePermissionsPermissionManager
from cms.models import signals as cms_signals
from cms.utils.page import get_available_slug
from cms.exceptions import NoHomeFound



class Page(Publisher, Mptt):
    """
    A simple hierarchical page model
    """
    MODERATOR_CHANGED = 0
    MODERATOR_NEED_APPROVEMENT = 1
    MODERATOR_APPROVED = 10
    # special case - page was approved, but some of page parents if not approved yet
    MODERATOR_APPROVED_WAITING_FOR_PARENTS = 11
    
    moderator_state_choices = (
        (MODERATOR_CHANGED, _('changed')),
        (MODERATOR_NEED_APPROVEMENT, _('req. app.')),
        (MODERATOR_APPROVED, _('approved')),
        (MODERATOR_APPROVED_WAITING_FOR_PARENTS, _('app. par.')),
    )
    created_by = models.CharField(_("created by"), max_length=70)
    changed_by = models.CharField(_("changed by"), max_length=70)
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children', db_index=True)
    creation_date = models.DateTimeField(editable=False, default=datetime.now)
    publication_date = models.DateTimeField(_("publication date"), null=True, blank=True, help_text=_('When the page should go live. Status must be "Published" for page to go live.'), db_index=True)
    publication_end_date = models.DateTimeField(_("publication end date"), null=True, blank=True, help_text=_('When to expire the page. Leave empty to never expire.'), db_index=True)
    login_required = models.BooleanField(_('login required'), default=False)
    in_navigation = models.BooleanField(_("in navigation"), default=True, db_index=True)
    soft_root = models.BooleanField(_("soft root"), db_index=True, default=False, help_text=_("All ancestors will not be displayed in the navigation"))
    reverse_id = models.CharField(_("id"), max_length=40, db_index=True, blank=True, null=True, help_text=_("An unique identifier that is used with the page_url templatetag for linking to this page"))
    navigation_extenders = models.CharField(_("navigation extenders"), max_length=80, db_index=True, blank=True, null=True, choices=settings.CMS_NAVIGATION_EXTENDERS)
    published = models.BooleanField(_("is published"), blank=True)
    
    template = models.CharField(_("template"), max_length=100, choices=settings.CMS_TEMPLATES, help_text=_('The template used to render the content.'))
    site = models.ForeignKey(Site, help_text=_('The site the page is accessible at.'), verbose_name=_("site"))
    
    moderator_state = models.SmallIntegerField(_('moderator state'), choices=moderator_state_choices, default=MODERATOR_NEED_APPROVEMENT, blank=True)
    
    level = models.PositiveIntegerField(db_index=True, editable=False)
    lft = models.PositiveIntegerField(db_index=True, editable=False)
    rght = models.PositiveIntegerField(db_index=True, editable=False)
    tree_id = models.PositiveIntegerField(db_index=True, editable=False)
    
    
    def get_hash(self):
        """Used in object comparison - if there were some change between objects
        generates sha1.
        """
        
    
    
    # Managers
    objects = PageManager()
    permissions = PagePermissionsPermissionManager()

    class Meta:
        verbose_name = _('page')
        verbose_name_plural = _('pages')
        ordering = ('tree_id', 'lft')
        app_label = 'cms'

    
    def __unicode__(self):
        slug = self.get_slug(fallback=True)
        if slug is None:
            return u'' # otherwise we get unicode decode errors
        else:
            return slug
        
    
    def move_page(self, target, position='first-child'):
        """Called from admin interface when page is moved. Should be used on
        all the places which are changing page position. Used like an interface
        to mptt, but after move is done page_moved signal is fired.
        """
        self.move_to(target, position)
        # fire signal
        from cms.models.moderatormodels import PageModeratorState
        self.force_moderation_action = PageModeratorState.ACTION_MOVE
        cms_signals.page_moved.send(sender=Page, instance=self) #titles get saved before moderation
        self.save(change_state=True) # always save the page after move, because of publisher
        
        
    def copy_page(self, target, site, position='first-child', copy_permissions=True, copy_moderation=True):
        """
        copy a page and all its descendants to a new location
        
        Doesn't checks for add page permissions anymore, this is done in PageAdmin.
        """
        from cms.utils.moderator import update_moderation_message
        
        descendants = [self] + list(self.get_descendants().order_by('-rght'))
        tree = [target]
        level_dif = self.level - target.level - 1
        first = True
        for page in descendants:
            new_level = page.level - level_dif
            dif = new_level - tree[-1].level 
            if dif < 0:
                tree = tree[:dif-1]
           
            titles = list(page.title_set.all())
            plugins = list(page.cmsplugin_set.all().order_by('tree_id', '-rght'))
            
            origin_id = page.id
            # IMPORTANT NOTE: self gets changed in next few lines to page!!
            
            page.pk = None
            page.level = None
            page.rght = None
            page.lft = None
            page.tree_id = None
            page.status = Page.MODERATOR_NEED_APPROVEMENT
            page.parent = tree[-1]
            page.public_id = None
            page.reverse_id = None
            page.save()
            
            update_moderation_message(page, _('Page was copied.'))
            # copy moderation, permissions if necessary
            if settings.CMS_PERMISSION and copy_permissions:
                from cms.models.permissionmodels import PagePermission
                for permission in PagePermission.objects.filter(page__id=origin_id):
                    permission.pk = None
                    permission.page = page
                    permission.save()
            
            if settings.CMS_MODERATOR and copy_moderation:
                from cms.models.moderatormodels import PageModerator
                for moderator in PageModerator.objects.filter(page__id=origin_id):
                    moderator.pk = None
                    moderator.page = page
                    moderator.save()
            
            if first:
                first = False
                page.move_to(target, position)
            page.site = site
            page.save()
            for title in titles:
                title.pk = None
                title.public_id = None
                title.page = page
                title.slug = get_available_slug(title)
                title.save()
            ptree = []
            for p in plugins:
                plugin, cls = p.get_plugin_instance()
                p.page = page
                p.pk = None
                p.id = None
                p.tree_id = None
                p.lft = None
                p.rght = None
                p.public_id = None
                p.inherited_public_id = None
                
                if p.parent:
                    pdif = p.level - ptree[-1].level
                    if pdif < 0:
                        ptree = ptree[:pdif-1]
                    p.parent = ptree[-1]
                    if pdif != 0:
                        ptree.append(p)
                else:
                    ptree = [p]
                p.level = None
                p.save()
                if plugin:
                    plugin.pk = p.pk
                    plugin.id = p.pk
                    plugin.page = page
                    plugin.tree_id = p.tree_id
                    plugin.lft = p.lft
                    plugin.rght = p.rght
                    plugin.level = p.level
                    plugin.cmsplugin_ptr = p
                    plugin.inherited_public_id = p.inherited_public_id
                    plugin.public_id = p.pk
                    plugin.save()
            if dif != 0:
                tree.append(page)
    
    def save(self, no_signals=False, change_state=True, commit=True, force_with_moderation=False):
        """
        Args:
            
            commit: True if model should be really saved
            force_with_moderation: can be true when new object gets added under 
                some existing page and this new page will require moderation; 
                this is because of how this adding works - first save, then move
        """
        # Published pages should always have a publication date
        publish_directly, under_moderation = False, False
        
        if settings.CMS_MODERATOR:
            under_moderation = force_with_moderation or self.pk and bool(self.get_moderator_queryset().count())
        
        
        created = not bool(self.pk)
        if settings.CMS_MODERATOR:
            if change_state:
                if self.moderator_state is not Page.MODERATOR_CHANGED:
                    # always change state to need approvement when there is some change
                    self.moderator_state = Page.MODERATOR_NEED_APPROVEMENT
                
                if not under_moderation:
                    # existing page without moderator - publish it directly
                    publish_directly = True
        elif change_state:
            self.moderator_state = Page.MODERATOR_CHANGED
            publish_directly = True
        
        if self.publication_date is None and self.published:
            self.publication_date = datetime.now()
        # Drafts should not, unless they have been set to the future
        if self.published:
            if settings.CMS_SHOW_START_DATE:
                if self.publication_date and self.publication_date <= datetime.now():
                    self.publication_date = None
            else:
                self.publication_date = None
        if self.reverse_id == "":
            self.reverse_id = None
        
        from cms.utils.permissions import _thread_locals
        
        self.changed_by = _thread_locals.user.username
        if not self.pk:
            self.created_by = self.changed_by 
        
        if commit:
            if no_signals:# ugly hack because of mptt
                super(Page, self).save_base(cls=self.__class__)
            else:
                super(Page, self).save()
        
        if publish_directly or created and not under_moderation:
            self.publish()
            cms_signals.post_publish.send(sender=Page, instance=self)

    def get_calculated_status(self):
        """
        get the calculated status of the page based on published_date,
        published_end_date, and status
        """
        if settings.CMS_SHOW_START_DATE:
            if self.publication_date > datetime.now():
                return False
        
        if settings.CMS_SHOW_END_DATE and self.publication_end_date:
            if self.publication_end_date < datetime.now():
                return True

        return self.published
    calculated_status = property(get_calculated_status)
        
    def get_languages(self):
        """
        get the list of all existing languages for this page
        """
        from cms.models.titlemodels import Title
        titles = Title.objects.filter(page=self)
        if not hasattr(self, "languages_cache"):
            languages = []
            for t in titles:
                if t.language not in languages:
                    languages.append(t.language)
            self.languages_cache = languages
        return self.languages_cache

    def get_absolute_url(self, language=None, fallback=True):
        try:
            if self.is_home():
                return reverse('pages-root')
        except NoHomeFound:
            pass
        if settings.CMS_FLAT_URLS:
            path = self.get_slug(language, fallback)
        else:
            path = self.get_path(language, fallback)
            home_pk = None
            try:
                home_pk = self.get_home_pk_cache()
            except NoHomeFound:
                pass
            ancestors = self.get_cached_ancestors()
            if self.parent_id and ancestors[0].pk == home_pk and not self.get_title_obj_attribute("has_url_overwrite", language, fallback):
                path = "/".join(path.split("/")[1:])
            
        return urljoin(reverse('pages-root'), path)
    
    def get_cached_ancestors(self, ascending=True):
        if ascending:
            if not hasattr(self, "ancestors_ascending"):
                self.ancestors_ascending = list(self.get_ancestors(ascending)) 
            return self.ancestors_ascending
        else:
            if not hasattr(self, "ancestors_descending"):
                self.ancestors_descending = list(self.get_ancestors(ascending))
            return self.ancestors_descending
    
    def get_title_obj(self, language=None, fallback=True, version_id=None, force_reload=False):
        """Helper function for accessing wanted / current title. 
        If wanted title doesn't exists, EmptyTitle instance will be returned.
        """
        self._get_title_cache(language, fallback, version_id, force_reload)
        if self.title_cache:
            return self.title_cache
        from cms.models.titlemodels import EmptyTitle
        return EmptyTitle()
    
    def get_title_obj_attribute(self, attrname, language=None, fallback=True, version_id=None, force_reload=False):
        """Helper function for getting attribute or None from wanted/current title.
        """
        try:
            return getattr(self.get_title_obj(language, fallback, version_id, force_reload), attrname)
        except AttributeError:
            return None
    
    def get_path(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get the path of the page depending on the given language
        """
        return self.get_title_obj_attribute("path", language, fallback, version_id, force_reload)

    def get_slug(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get the slug of the page depending on the given language
        """
        return self.get_title_obj_attribute("slug", language, fallback, version_id, force_reload)
        
    def get_title(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get the title of the page depending on the given language
        """
        return self.get_title_obj_attribute("title", language, fallback, version_id, force_reload)
    
    def get_menu_title(self, language=None, fallback=False, version_id=None, force_reload=False):
        """
        get the menu title of the page depending on the given language
        """
        menu_title = self.get_title_obj_attribute("menu_title", language, fallback, version_id, force_reload)
        if not menu_title:
            return self.get_title(language, True, version_id, force_reload)
        return menu_title
    
    def get_page_title(self, language=None, fallback=False, version_id=None, force_reload=False):
        """
        get the page title of the page depending on the given language
        """
        page_title = self.get_title_obj_attribute("page_title", language, fallback, version_id, force_reload)
        if not page_title:
            return self.get_menu_title(language, True, version_id, force_reload)
        return page_title

    def get_meta_description(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get content for the description meta tag for the page depending on the given language
        """
        return self.get_title_obj_attribute("meta_description", language, fallback, version_id, force_reload)

    def get_meta_keywords(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get content for the keywords meta tag for the page depending on the given language
        """
        return self.get_title_obj_attribute("meta_keywords", language, fallback, version_id, force_reload)
        
    def get_application_urls(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get application urls conf for application hook
        """
        return self.get_title_obj_attribute("application_urls", language, fallback, version_id, force_reload)
    
    def get_redirect(self, language=None, fallback=True, version_id=None, force_reload=False):
        """
        get redirect
        """
        return self.get_title_obj_attribute("redirect", language, fallback, version_id, force_reload)
    
    def _get_title_cache(self, language, fallback, version_id, force_reload):
        default_lang = False
        if not language:
            default_lang = True
            language = settings.CMS_DEFAULT_LANGUAGE
        load = False
        if not hasattr(self, "title_cache"):
            load = True
        elif self.title_cache and self.title_cache.language != language and language and not default_lang:
            load = True
        elif fallback and not self.title_cache:
            load = True 
        if force_reload:
            load = True
        if load:
            from cms.models.titlemodels import Title
            if version_id:
                from reversion.models import Version
                version = get_object_or_404(Version, pk=version_id)
                revs = [related_version.object_version for related_version in version.revision.version_set.all()]
                for rev in revs:
                    obj = rev.object
                    if obj.__class__ == Title:
                        if obj.language == language and obj.page_id == self.pk:
                            self.title_cache = obj
                if not self.title_cache and fallback:
                    for rev in revs:
                        obj = rev.object
                        if obj.__class__ == Title:
                            if obj.page_id == self.pk:
                                self.title_cache = obj
            else:
                self.title_cache = Title.objects.get_title(self, language, language_fallback=fallback)
                
    def get_template(self):
        """
        get the template of this page.
        """
        return self.template

    def get_template_name(self):
        """
        get the template of this page if defined or if closer parent if
        defined or DEFAULT_PAGE_TEMPLATE otherwise
        """
        template = None
        if self.template:
            template = self.template
        if not template:
            for p in self.get_ancestors(ascending=True):
                if p.template:
                    template =  p.template
                    break
        if not template:
            template = settings.CMS_TEMPLATES[0][0]
        for t in settings.CMS_TEMPLATES:
            if t[0] == template:
                return t[1] 
        return _("default")

    #def traductions(self):
    #    langs = ""
    #    for lang in self.get_languages():
    #        langs += '%s, ' % lang
    #    return langs[0:-2]

    def has_change_permission(self, request):
        opts = self._meta
        if request.user.is_superuser:
            return True
        return request.user.has_perm(opts.app_label + '.' + opts.get_change_permission()) and \
            self.has_generic_permission(request, "change")
    
    def has_delete_permission(self, request):
        opts = self._meta
        if request.user.is_superuser:
            return True
        return request.user.has_perm(opts.app_label + '.' + opts.get_delete_permission()) and \
            self.has_generic_permission(request, "delete")
    
    def has_publish_permission(self, request):
        return self.has_generic_permission(request, "publish")
    
    def has_advanced_settings_permission(self, request):
        return self.has_generic_permission(request, "advanced_settings")
    
    def has_change_permissions_permission(self, request):
        """Has user ability to change permissions for current page?
        """
        return self.has_generic_permission(request, "change_permissions")
    
    def has_add_permission(self, request):
        """Has user ability to add page under current page?
        """
        return self.has_generic_permission(request, "add")
    
    def has_move_page_permission(self, request):
        """Has user ability to move current page?
        """
        return self.has_generic_permission(request, "move_page")
    
    def has_moderate_permission(self, request):
        """Has user ability to moderate current page? If moderation isn't 
        installed, nobody can moderate.
        """
        if not settings.CMS_MODERATOR:
            return False
        return self.has_generic_permission(request, "moderate")
    
    def has_generic_permission(self, request, type):
        """
        Return true if the current user has permission on the page.
        Return the string 'All' if the user has all rights.
        """
        att_name = "permission_%s_cache" % type
        if not hasattr(self, "permission_user_cache") or not hasattr(self, att_name) \
            or request.user.pk != self.permission_user_cache.pk:
            
            from cms.utils.permissions import has_generic_permission
            self.permission_user_cache = request.user
            setattr(self, att_name, has_generic_permission(self.id, request.user, type))
            if getattr(self, att_name):
                self.permission_edit_cache = True
        return getattr(self, att_name)
    
    def is_home(self):
        if self.parent_id:
            return False
        else:
            try:
                return self.get_home_pk_cache() == self.pk
            except NoHomeFound:
                pass
        return False
        
    def is_parent_home(self):
        if not self.parent_id:
            return False
        else:
            try:
                return self.get_home_pk_cache() == self.parent_id
            except NoHomeFound:
                pass
        return False
        
    def get_home_pk_cache(self):
        if not hasattr(self, "home_pk_cache"):
            self.home_pk_cache = Page.objects.get_home().pk
        return self.home_pk_cache
            
    def get_media_path(self, filename):
        """
        Returns path (relative to MEDIA_ROOT/MEDIA_URL) to directory for storing page-scope files.
        This allows multiple pages to contain files with identical names without namespace issues.
        Plugins such as Picture can use this method to initialise the 'upload_to' parameter for 
        File-based fields. For example:
            image = models.ImageField(_("image"), upload_to=CMSPlugin.get_media_path)
        where CMSPlugin.get_media_path calls self.page.get_media_path
        
        This location can be customised using the CMS_PAGE_MEDIA_PATH setting
        """
        return join(settings.CMS_PAGE_MEDIA_PATH, "%d" % self.id, filename)
    
    def last_page_state(self):
        """Returns last page state if CMS_MODERATOR
        """
        
        # TODO: optimize SQL... 1 query per page 
        if settings.CMS_MODERATOR:
            # unknown state if no moderator
            try:
                return self.pagemoderatorstate_set.all().order_by('-created',)[0]
            except IndexError:
                pass
        return None
    
    def get_moderator_queryset(self):
        """Returns ordered set of all PageModerator instances, which should 
        moderate this page
        """
        from cms.models.moderatormodels import PageModerator
        if not settings.CMS_MODERATOR or not self.tree_id:
            return PageModerator.objects.get_empty_query_set()
        
        q = Q(page__tree_id=self.tree_id, page__level__lt=self.level, moderate_descendants=True) | \
            Q(page__tree_id=self.tree_id, page__level=self.level - 1, moderate_children=True) | \
            Q(page__pk=self.pk, moderate_page=True)
        
        return PageModerator.objects.distinct().filter(q).order_by('page__level')
    
    def is_under_moderation(self):
        return bool(self.get_moderator_queryset().count())
    
    def is_approved(self):
        """Returns true, if page is approved and published, or approved, but
        parents are missing..
        """
        return self.moderator_state in (Page.MODERATOR_APPROVED, Page.MODERATOR_APPROVED_WAITING_FOR_PARENTS)
    
    def publish(self, fields=None, exclude=None):
        """Overrides Publisher method, because there may be some descendants, which
        are waiting for parent to publish, so publish them if possible. 
        
        IMPORTANT: @See utils.moderator.approve_page for publishing permissions
        
        Returns: True if page was successfully published.
        """
        if not settings.CMS_MODERATOR:
            return
        
        # clean moderation log
        self.pagemoderatorstate_set.all().delete()
        
        # can be this page published?
        if self.mptt_can_publish():
            self.moderator_state = Page.MODERATOR_APPROVED
        else:
            self.moderator_state = Page.MODERATOR_APPROVED_WAITING_FOR_PARENTS
        
        self.save(change_state=False)
        
        if not fields:
            public = self.public
            if public:
                if public.tree_id != self.tree_id: #moved over trees
                    tree_ids = [self.tree_id, public.tree_id]
                else:
                    tree_ids = [self.tree_id]
                
                dirty = False
                if self.lft != public.lft or self.rght != public.rght or self.level != public.level:#moved in tree
                    dirty = True
                if dirty or len(tree_ids) == 2:
                    pages = list(Page.objects.filter(tree_id__in=tree_ids).order_by("tree_id", "level", "lft"))
                    fields = []
                    names = ["lft","rght","tree_id", "level", "parent", "created_by", "changed_by", "site"]
                    for field in self._meta.fields:
                        if field.name in names:
                            fields.append(field)
                    ids = []
                    for page in pages:
                        if page.pk != self.pk:
                            page.publish(fields=fields)
                            ids.append(page.pk)
                    from cms.models.titlemodels import Title
                    titles = Title.objects.filter(page__in=ids)
                    title_fields = []
                    names = ["path"]
                    for field in Title._meta.fields:
                        if field.name in names:
                            title_fields.append(field)
                    for title in titles:
                        title.publish(fields=title_fields)
            else:
                pass
                #print "no public found"            
        
        # publish, but only if all parents are published!! - this will need a flag
        try:
            published = super(Page, self).publish(fields, exclude)
        except MpttCantPublish:
            return 
        
        # page was published, check if there are some childs, which are waiting
        # for publishing (because of the parent)
        publish_set = self.children.filter(moderator_state = Page.MODERATOR_APPROVED_WAITING_FOR_PARENTS)
        for page in publish_set:
            # recursive call to all childrens....
            page.moderator_state = Page.MODERATOR_APPROVED
            page.save(change_state=False)
            page.publish()
        
        # fire signal after publishing is done
        return published
    
    def is_public_published(self):
        """Returns true if public model is published.
        """
        if hasattr(self, 'public_published_cache'):
            # if it was cached in change list, return cached value
            return self.public_published_cache
        # othervise make db lookup
        if self.public:
            return self.public.published
        #return is_public_published(self)
        return False
        

    
if 'reversion' in settings.INSTALLED_APPS: 
    import reversion       
    reversion.register(Page, follow=["title_set", "cmsplugin_set", "pagepermission_set"])