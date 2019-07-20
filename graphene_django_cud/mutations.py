from collections import OrderedDict

import graphene
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db import models
from graphene import Mutation, InputObjectType
from graphene.types.mutation import MutationOptions
from graphene.types.utils import yank_fields_from_attrs
from graphene.utils.str_converters import to_snake_case
from graphene_django.registry import get_global_registry
from graphql import GraphQLError
from graphql_relay import to_global_id

from graphene_django_cud.registry import get_type_meta_registry
from .util import disambiguate_id, disambiguate_ids, get_input_fields_for_model, \
    get_all_optional_input_fields_for_model, is_many_to_many, get_m2m_all_extras_field_names, \
    get_likely_operation_from_name, get_fk_all_extras_field_names


class DjangoCudBase(Mutation):
    class Meta:
        abstract = True

    @classmethod
    def create_obj(
            cls,
            input,
            info,
            auto_context_fields,
            many_to_many_extras,
            foreign_key_extras,
            Model
    ):
        meta_registry = get_type_meta_registry()

        model_field_values = {}
        many_to_many_values = {}

        many_to_many_extras_field_names = get_m2m_all_extras_field_names(many_to_many_extras)
        foreign_key_extras_field_names = get_fk_all_extras_field_names(foreign_key_extras)

        for field_name, context_name in cls._meta.auto_context_fields.items():
            if hasattr(info.context, context_name):
                model_field_values[field_name] = getattr(info.context, context_name)

        for name, value in input.items():
            # Handle these separately
            if name in many_to_many_extras_field_names or name in foreign_key_extras_field_names:
                continue

            field = Model._meta.get_field(name)
            new_value = value

            # We have to handle this case specifically, by using the fields
            # .set()-method, instead of direct assignment
            field_is_many_to_many = is_many_to_many(field)

            value_handle_name = "handle_" + name
            if hasattr(cls, value_handle_name):
                handle_func = getattr(cls, value_handle_name)
                assert callable(
                    handle_func
                ), f"Property {value_handle_name} on {cls.__name__} is not a function."
                new_value = handle_func(value, name, info)

            # On some fields we perform some default conversion, if the value was not transformed above.
            if new_value == value and value is not None:
                if type(field) in (models.ForeignKey, models.OneToOneField):
                    # Delete auto context field here, if it exists. We have to do this explicitly
                    # as we change the name below
                    if name in auto_context_fields:
                        del model_field_values[name]

                    name = getattr(field, "db_column", None) or name + "_id"
                    new_value = disambiguate_id(value)
                elif field_is_many_to_many:
                    new_value = disambiguate_ids(value)


            if field_is_many_to_many:
                many_to_many_values[name] = new_value
            else:
                model_field_values[name] = new_value

        # We don't have an object yet, and we potentially need to create a
        # parent before proceeding.
        for name, extras in foreign_key_extras.items():
            value = input.get(name, None)
            field = Model._meta.get_field(name)
            field_type = extras.get('type', 'ID')

            if field_type == "ID":
                model_field_values[name + "_id"] = value
            else:
                input_type_meta = meta_registry.get_meta_for_type(field_type)
                # Create new obj
                related_obj = cls.create_obj(
                    value,
                    info,
                    input_type_meta.get('auto_context_fields', {}),
                    input_type_meta.get('many_to_many_extras', {}),
                    input_type_meta.get('foreign_key_extras', {}),
                    field.related_model
                )
                model_field_values[name] = related_obj

        print(model_field_values)
        # Foreign keys are added, we are ready to create our object
        obj = Model.objects.create(**model_field_values)

        for name, values in many_to_many_values.items():
            getattr(obj, name).set(values)

        # Handle extras fields
        many_to_many_to_add = {}
        many_to_many_to_remove = {}
        for name, extras in many_to_many_extras.items():
            field = Model._meta.get_field(name)
            if not name in many_to_many_to_add:
                many_to_many_to_add[name] = []
                many_to_many_to_remove[name] = []

            for extra_name, data in extras.items():
                field_name = name
                if extra_name != "exact":
                    field_name = name + "_" + extra_name

                values = input.get(field_name, None)
                if values is None:
                    continue

                if isinstance(data, bool):
                    data = {}

                field_type = data.get('type', 'ID')
                operation = data.get('operation') or get_likely_operation_from_name(extra_name)

                for value in values:
                    if field_type == "ID":
                        related_obj = field.related_model.objects.get(pk=disambiguate_id(value))
                    else:
                        # This is something that we are going to create
                        input_type_meta = meta_registry.get_meta_for_type(field_type)
                        # Create new obj
                        related_obj = cls.create_obj(
                            value,
                            info,
                            input_type_meta.get('auto_context_fields', {}),
                            input_type_meta.get('many_to_many_extras', {}),
                            input_type_meta.get('foreign_key_extras', {}),
                            field.related_model
                        )

                    if operation == "add":
                        many_to_many_to_add[name].append(related_obj)
                    else:
                        many_to_many_to_remove[name].append(related_obj)

        for name, objs in many_to_many_to_add.items():
            getattr(obj, name).add(*objs)

        for name, objs in many_to_many_to_remove.items():
            getattr(obj, name).remove(*objs)

        return obj


    @classmethod
    def update_obj(
            cls,
            obj,
            input,
            info,
            auto_context_fields,
            many_to_many_extras,
            foreign_key_extras,
            Model
    ):
        meta_registry = get_type_meta_registry()

        many_to_many_values = {}
        many_to_many_add_values = {}
        many_to_many_remove_values = {}

        many_to_many_extras_field_names = get_m2m_all_extras_field_names(many_to_many_extras)
        foreign_key_extras_field_names = get_fk_all_extras_field_names(foreign_key_extras)

        for field_name, context_name in auto_context_fields.items():
            if hasattr(info.context, context_name):
                setattr(obj, field_name, getattr(info.context, context_name))

        for name, value in input.items():

            # Handle these separately
            if name in many_to_many_extras_field_names or name in foreign_key_extras_field_names:
                continue

            field = Model._meta.get_field(name)
            new_value = value

            # We have to handle this case specifically, by using the fields
            # .set()-method, instead of direct assignment
            field_is_many_to_many = is_many_to_many(field)

            value_handle_name = "handle_" + name
            if hasattr(cls, value_handle_name):
                handle_func = getattr(cls, value_handle_name)
                assert callable(
                    handle_func
                ), f"Property {value_handle_name} on {cls.__name__} is not a function."
                new_value = handle_func(value, name, info)

            # On some fields we perform some default conversion, if the value was not transformed above.
            if new_value == value and value is not None:
                if type(field) in (models.ForeignKey, models.OneToOneField):
                    # Delete auto context field here, if it exists. We have to do this explicitly
                    # as we change the name below
                    if name in auto_context_fields:
                        setattr(obj, name, None)

                    name = getattr(field, "db_column", None) or name + "_id"
                    new_value = disambiguate_id(value)
                elif field_is_many_to_many:
                    new_value = disambiguate_ids(value)

            if field_is_many_to_many:
                many_to_many_values[name] = new_value
            else:
                setattr(obj, name, new_value)

        # Handle extras fields
        for name, extras in foreign_key_extras.items():
            value = input.get(name, None)
            field = Model._meta.get_field(name)
            field_type = extras.get('type', 'ID')

            if field_type == "ID":
                setattr(obj, name, value)
            else:
                input_type_meta = meta_registry.get_meta_for_type(field_type)
                # Create new obj
                related_obj = cls.create_obj(
                    value,
                    info,
                    input_type_meta.get('auto_context_fields', {}),
                    input_type_meta.get('many_to_many_extras', {}),
                    input_type_meta.get('foreign_key_extras', {}),
                    field.related_model
                )
                setattr(obj, name, value)

        many_to_many_to_add = {}
        many_to_many_to_remove = {}
        for name, extras in many_to_many_extras.items():
            field = Model._meta.get_field(name)
            if not name in many_to_many_to_add:
                many_to_many_to_add[name] = []
                many_to_many_to_remove[name] = []

            for extra_name, data in extras.items():
                field_name = name + "_" + extra_name

                values = input.get(field_name, None)
                if values is None:
                    continue

                if isinstance(data, bool):
                    data = {}

                field_type = data.get('type', 'ID')
                operation = data.get('operation') or get_likely_operation_from_name(extra_name)

                for value in values:
                    if field_type == "ID":
                        related_obj = field.related_model.objects.get(pk=disambiguate_id(value))
                    else:
                        # This is something that we are going to create
                        input_type_meta = meta_registry.get_meta_for_type(field_type)
                        # Create new obj
                        related_obj = cls.create_obj(
                            value,
                            info,
                            input_type_meta.get('auto_context_fields', {}),
                            input_type_meta.get('many_to_many_extras', {}),
                            input_type_meta.get('foreign_key_extras', {}),
                            field.related_model
                        )

                    if operation == "add":
                        many_to_many_to_add[name].append(related_obj)
                    else:
                        many_to_many_to_remove[name].append(related_obj)

        for name, objs in many_to_many_to_add.items():
            getattr(obj, name).add(*objs)

        for name, objs in many_to_many_to_remove.items():
            getattr(obj, name).remove(*objs)

        return obj


class DjangoUpdateMutationOptions(MutationOptions):
    model = None
    only_fields = None
    exclude_fields = None
    return_field_name = None
    permissions = None
    login_required = None
    auto_context_fields = None
    optional_fields = ()
    required_fields = None
    nested_fields = None

    many_to_many_extras = None
    foreign_key_extras = None


class DjangoUpdateMutation(DjangoCudBase):
    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(
            cls,
            model=None,
            permissions=None,
            login_required=None,
            only_fields=(),
            exclude_fields=(),
            auto_context_fields={},
            optional_fields=(),
            required_fields=(),
            return_field_name=None,
            many_to_many_extras=None,
            foreign_key_extras=None,
            **kwargs,
    ):
        registry = get_global_registry()
        meta_registry = get_type_meta_registry()
        model_type = registry.get_type_for_model(model)

        assert model_type, f"Model type must be registered for model {model}"

        if not return_field_name:
            return_field_name = to_snake_case(model.__name__)

        model_fields = get_input_fields_for_model(
            model,
            only_fields,
            exclude_fields,
            optional_fields=tuple(auto_context_fields.keys()) + optional_fields,
            required_fields=required_fields,
            many_to_many_extras=many_to_many_extras,
            foreign_key_extras=foreign_key_extras
        )

        input_type_name = f"Update{model.__name__}Input"

        InputType = type(
            input_type_name, (InputObjectType,), model_fields
        )

        # Register meta-data
        meta_registry.register(
            InputType,
            {
                'auto_context_fields': auto_context_fields or {},
                'optional_fields': optional_fields,
                'required_fields': required_fields,
                'many_to_many_extras': many_to_many_extras or {},
                'foreign_key_extras': foreign_key_extras or {}
            }
        )

        registry.register_converted_field(
            input_type_name,
            InputType
        )

        arguments = OrderedDict(
            id=graphene.ID(required=True), input=InputType(required=True)
        )

        output_fields = OrderedDict()
        output_fields[return_field_name] = graphene.Field(model_type)

        _meta = DjangoUpdateMutationOptions(cls)
        _meta.model = model
        _meta.fields = yank_fields_from_attrs(output_fields, _as=graphene.Field)
        _meta.return_field_name = return_field_name
        _meta.permissions = permissions
        _meta.auto_context_fields = auto_context_fields or {}
        _meta.optional_fields = optional_fields
        _meta.required_fields = required_fields
        _meta.InputType = InputType
        _meta.input_type_name = input_type_name
        _meta.many_to_many_extras = many_to_many_extras
        _meta.foreign_key_extras = foreign_key_extras
        _meta.login_required = _meta.login_required or (
                _meta.permissions and len(_meta.permissions) > 0
        )

        super().__init_subclass_with_meta__(arguments=arguments, _meta=_meta, **kwargs)

    def get_queryset(self):
        Model = self._meta.model
        return Model.objects

    @classmethod
    def mutate(cls, root, info, id, input):
        if cls._meta.login_required and not info.context.user.is_authenticated:
            raise GraphQLError("Must be logged in to access this mutation.")

        if cls._meta.permissions and len(cls._meta.permissions) > 0:
            if not info.context.user.has_perms(cls._meta.permissions):
                raise GraphQLError("Not permitted to access this mutation.")

        id = disambiguate_id(id)
        Model = cls._meta.model
        queryset = cls.get_queryset(Model)
        obj = queryset.get(pk=id)
        auto_context_fields = cls._meta.auto_context_fields or {}

        obj = cls.update_obj(
            obj,
            input,
            info,
            auto_context_fields,
            cls._meta.many_to_many_extras,
            cls._meta.foreign_key_extras,
            Model
        )

        obj.save()
        kwargs = {cls._meta.return_field_name: obj}

        return cls(**kwargs)


class DjangoPatchMutationOptions(MutationOptions):
    model = None
    only_fields = None
    exclude_fields = None
    return_field_name = None
    permissions = None
    login_required = None
    auto_context_fields = None
    many_to_many_extras = None
    foreign_key_extras = None


class DjangoPatchMutation(DjangoCudBase):
    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(
            cls,
            model=None,
            permissions=None,
            login_required=None,
            only_fields=(),
            exclude_fields=(),
            return_field_name=None,
            auto_context_fields={},
            many_to_many_extras = None,
            foreign_key_extras = None,
            **kwargs,
    ):
        registry = get_global_registry()
        meta_registry = get_type_meta_registry()
        model_type = registry.get_type_for_model(model)

        assert model_type, f"Model type must be registered for model {model}"

        if not return_field_name:
            return_field_name = to_snake_case(model.__name__)

        model_fields = get_all_optional_input_fields_for_model(
            model,
            only_fields,
            exclude_fields,
            many_to_many_extras=many_to_many_extras,
            foreign_key_extras=foreign_key_extras
        )

        input_type_name = f"Patch{model.__name__}Input"

        InputType = type(
            input_type_name, (InputObjectType,), model_fields
        )

        # Register meta-data
        meta_registry.register(
            InputType,
            {
                'auto_context_fields': auto_context_fields or {},
                'many_to_many_extras': many_to_many_extras or {},
                'foreign_key_extras': foreign_key_extras or {}
            }
        )

        registry.register_converted_field(
            input_type_name,
            InputType
        )

        arguments = OrderedDict(
            id=graphene.ID(required=True), input=InputType(required=True)
        )

        output_fields = OrderedDict()
        output_fields[return_field_name] = graphene.Field(model_type)

        _meta = DjangoPatchMutationOptions(cls)
        _meta.model = model
        _meta.fields = yank_fields_from_attrs(output_fields, _as=graphene.Field)
        _meta.return_field_name = return_field_name
        _meta.permissions = permissions
        _meta.auto_context_fields = auto_context_fields or {}
        _meta.InputType = InputType
        _meta.input_type_name = input_type_name
        _meta.many_to_many_extras = many_to_many_extras
        _meta.foreign_key_extras = foreign_key_extras
        _meta.login_required = _meta.login_required or (
                _meta.permissions and len(_meta.permissions) > 0
        )

        super().__init_subclass_with_meta__(arguments=arguments, _meta=_meta, **kwargs)

    def get_queryset(self):
        Model = self._meta.model
        return Model.objects

    @classmethod
    def mutate(cls, root, info, id, input):
        if cls._meta.login_required and not info.context.user.is_authenticated:
            raise GraphQLError("Must be logged in to access this mutation.")

        if cls._meta.permissions and len(cls._meta.permissions) > 0:
            if not info.context.user.has_perms(cls._meta.permissions):
                raise GraphQLError("Not permitted to access this mutation.")

        id = disambiguate_id(id)
        Model = cls._meta.model
        queryset = cls.get_queryset(Model)
        obj = queryset.get(pk=id)
        auto_context_fields = cls._meta.auto_context_fields or {}

        obj = cls.update_obj(
            obj,
            input,
            info,
            auto_context_fields,
            cls._meta.many_to_many_extras,
            cls._meta.foreign_key_extras,
            Model
        )

        obj.save()
        kwargs = {cls._meta.return_field_name: obj}

        return cls(**kwargs)


class DjangoCreateMutationOptions(MutationOptions):
    model = None
    only_fields = None
    exclude_fields = None
    return_field_name = None
    permissions = None
    login_required = None
    auto_context_fields = None
    optional_fields = ()
    required_fields = ()
    many_to_many_extras = None
    foreign_key_extras = None


class DjangoCreateMutation(DjangoCudBase):
    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(
            cls,
            model=None,
            permissions=None,
            login_required=None,
            only_fields=(),
            exclude_fields=(),
            optional_fields=(),
            required_fields=(),
            auto_context_fields={},
            return_field_name=None,
            many_to_many_extras = None,
            foreign_key_extras = None,
            **kwargs,
    ):
        registry = get_global_registry()
        meta_registry = get_type_meta_registry()
        model_type = registry.get_type_for_model(model)

        assert model_type, f"Model type must be registered for model {model}"

        if not return_field_name:
            return_field_name = to_snake_case(model.__name__)

        model_fields = get_input_fields_for_model(
            model,
            only_fields,
            exclude_fields,
            tuple(auto_context_fields.keys()) + optional_fields,
            required_fields,
            many_to_many_extras,
            foreign_key_extras
        )

        input_type_name = f"Create{model.__name__}Input"

        InputType = type(
            input_type_name, (InputObjectType,), model_fields
        )

        # Register meta-data
        meta_registry.register(
            InputType,
            {
                'auto_context_fields': auto_context_fields or {},
                'optional_fields': optional_fields,
                'required_fields': required_fields,
                'many_to_many_extras': many_to_many_extras or {},
                'foreign_key_extras': foreign_key_extras or {}
            }
        )

        registry.register_converted_field(
            input_type_name,
            InputType
        )

        arguments = OrderedDict(input=InputType(required=True))

        output_fields = OrderedDict()
        output_fields[return_field_name] = graphene.Field(model_type)

        _meta = DjangoCreateMutationOptions(cls)
        _meta.model = model
        _meta.fields = yank_fields_from_attrs(output_fields, _as=graphene.Field)
        _meta.return_field_name = return_field_name
        _meta.optional_fields = optional_fields
        _meta.required_fields = required_fields
        _meta.permissions = permissions
        _meta.auto_context_fields = auto_context_fields or {}
        _meta.many_to_many_extras = many_to_many_extras
        _meta.foreign_key_extras = foreign_key_extras
        _meta.InputType = InputType
        _meta.input_type_name = input_type_name
        _meta.login_required = _meta.login_required or (
                _meta.permissions and len(_meta.permissions) > 0
        )

        super().__init_subclass_with_meta__(arguments=arguments, _meta=_meta, **kwargs)

    @classmethod
    def mutate(cls, root, info, input):
        if cls._meta.login_required and not info.context.user.is_authenticated:
            raise GraphQLError("Must be logged in to access this mutation.")

        if cls._meta.permissions and len(cls._meta.permissions) > 0:
            if not info.context.user.has_perms(cls._meta.permissions):
                raise GraphQLError("Not permitted to access this mutation.")

        Model = cls._meta.model
        model_field_values = {}
        auto_context_fields = cls._meta.auto_context_fields or {}

        obj = cls.create_obj(
            input,
            info,
            auto_context_fields,
            cls._meta.many_to_many_extras,
            cls._meta.foreign_key_extras,
            Model
        )

        kwargs = {cls._meta.return_field_name: obj}
        return cls(**kwargs)


class DjangoDeleteMutationOptions(MutationOptions):
    model = None
    permissions = None
    login_required = None


class DjangoDeleteMutation(Mutation):
    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(
            cls,
            model=None,
            permissions=None,
            login_required=None,
            only_fields=(),
            exclude_fields=(),
            return_field_name=None,
            **kwargs,
    ):
        registry = get_global_registry()

        if not return_field_name:
            return_field_name = to_snake_case(model.__name__)

        arguments = OrderedDict(id=graphene.ID(required=True))

        output_fields = OrderedDict()
        output_fields["found"] = graphene.Boolean()
        output_fields["deleted_id"] = graphene.ID()

        _meta = DjangoDeleteMutationOptions(cls)
        _meta.model = model
        _meta.fields = yank_fields_from_attrs(output_fields, _as=graphene.Field)
        _meta.return_field_name = return_field_name
        _meta.permissions = permissions
        _meta.login_required = _meta.login_required or (
                _meta.permissions and len(_meta.permissions) > 0
        )

        super().__init_subclass_with_meta__(arguments=arguments, _meta=_meta, **kwargs)

    @classmethod
    def mutate(cls, root, info, id):
        if cls._meta.login_required and not info.context.user.is_authenticated:
            raise GraphQLError("Must be logged in to access this mutation.")

        if cls._meta.permissions and len(cls._meta.permissions) > 0:
            if not info.context.user.has_perms(cls._meta.permissions):
                raise GraphQLError("Not permitted to access this mutation.")

        Model = cls._meta.model
        id = disambiguate_id(id)

        try:
            obj = Model.objects.get(pk=id)
            obj.delete()
            return cls(found=True, deleted_id=id)
        except ObjectDoesNotExist:
            return cls(found=False)


class DjangoBatchDeleteMutationOptions(MutationOptions):
    model = None
    filter_fields = None
    filter_class = None
    permissions = None
    login_required = None


class DjangoBatchDeleteMutation(Mutation):
    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(
            cls,
            model=None,
            permissions=None,
            login_required=None,
            filter_fields=(),
            filter_class=None,
            **kwargs,
    ):
        registry = get_global_registry()
        model_type = registry.get_type_for_model(model)

        assert model_type, f"Model type must be registered for model {model}"
        assert (
                len(filter_fields) > 0
        ), f"You must specify at least one field to filter on for deletion."

        input_arguments = OrderedDict()
        for field in filter_fields:
            input_arguments[field] = graphene.String()

        InputType = type(
            f"BatchDelete{model.__name__}Input", (InputObjectType,), input_arguments
        )

        arguments = OrderedDict(input=InputType(required=True))

        output_fields = OrderedDict()
        output_fields["deletion_count"] = graphene.Int()
        output_fields["deleted_ids"] = graphene.List(graphene.ID)

        _meta = DjangoBatchDeleteMutationOptions(cls)
        _meta.model = model
        _meta.fields = yank_fields_from_attrs(output_fields, _as=graphene.Field)
        _meta.filter_fields = filter_fields
        _meta.permissions = permissions
        _meta.login_required = _meta.login_required or (
                _meta.permissions and len(_meta.permissions) > 0
        )

        super().__init_subclass_with_meta__(arguments=arguments, _meta=_meta, **kwargs)

    @classmethod
    def mutate(cls, root, info, input):
        if cls._meta.login_required and not info.context.user.is_authenticated:
            raise GraphQLError("Must be logged in to access this mutation.")

        if cls._meta.permissions and len(cls._meta.permissions) > 0:
            if not info.context.user.has_perms(cls._meta.permissions):
                raise GraphQLError("Not permitted to access this mutation.")

        Model = cls._meta.model
        model_field_values = {}

        for name, value in super(type(input), input).items():
            try:
                field = Model._meta.get_field(name)
            except FieldDoesNotExist:
                # This can happen with nested selectors. In this case we set the field to none.
                field = None

            new_value = value

            value_handle_name = "handle_" + name
            if hasattr(cls, value_handle_name):
                handle_func = getattr(cls, value_handle_name)
                assert callable(
                    handle_func
                ), f"Property {value_handle_name} on {cls.__name__} is not a function."
                new_value = handle_func(value, name, info)

            # On some fields we perform some default conversion, if the value was not transformed above.
            if new_value == value and field is not None and value is not None:
                if type(field) in (models.ForeignKey, models.OneToOneField):
                    name = getattr(field, "db_column", None) or name + "_id"
                    new_value = disambiguate_id(value)
                elif type(field) in (
                        models.ManyToManyField,
                        models.ManyToManyRel,
                        models.ManyToOneRel,
                ):
                    new_value = disambiguate_ids(value)

            model_field_values[name] = new_value

        filter_qs = Model.objects.filter(**model_field_values)
        ids = [
            to_global_id(get_global_registry().get_type_for_model(Model).__name__, id)
            for id in filter_qs.values_list("id", flat=True)
        ]
        deletion_count, _ = filter_qs.delete()

        return cls(deletion_count=deletion_count, deleted_ids=ids)

