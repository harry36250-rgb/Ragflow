import { ButtonLoading } from '@/components/ui/button';
import { useUpdateKnowledge } from '@/hooks/use-knowledge-request';
import { useMemo } from 'react';
import { useFormContext } from 'react-hook-form';
import { useTranslation } from 'react-i18next';
import { useParams } from 'umi';

export function GeneralSavingButton() {
  const form = useFormContext();
  const { saveKnowledgeConfiguration, loading: submitLoading } =
    useUpdateKnowledge();
  const { id: kb_id } = useParams();
  const { t } = useTranslation();

  const defaultValues = useMemo(
    () => form.formState.defaultValues ?? {},
    [form.formState.defaultValues],
  );
  const parser_id = defaultValues['parser_id'];

  return (
    <ButtonLoading
      type="button"
      loading={submitLoading}
      onClick={() => {
        (async () => {
          let isValidate = await form.trigger('name');
          const { name, description, permission, avatar } = form.getValues();

          if (isValidate) {
            saveKnowledgeConfiguration({
              kb_id,
              parser_id,
              name,
              description,
              avatar,
              permission,
            });
          }
        })();
      }}
    >
      {t('knowledgeConfiguration.save')}
    </ButtonLoading>
  );
}

export function SavingButton() {
  const { saveKnowledgeConfiguration, loading: submitLoading } =
    useUpdateKnowledge();
  const form = useFormContext();
  const { id: kb_id } = useParams();
  const { t } = useTranslation();

  return (
    <ButtonLoading
      loading={submitLoading}
      onClick={() => {
        (async () => {
          try {
            let beValid = false;
            try {
              const validationPromise = form.trigger();
              const timeoutPromise = new Promise((_, reject) =>
                setTimeout(
                  () => reject(new Error('Validation timeout after 5 seconds')),
                  5000,
                ),
              );

              beValid = (await Promise.race([
                validationPromise,
                timeoutPromise,
              ])) as boolean;
            } catch (validationError) {
              beValid = false;
            }

            if (beValid) {
              const values = form.getValues();
              delete values['parseType'];
              // delete values['avatar'];

              await saveKnowledgeConfiguration({
                kb_id,
                ...values,
              });
            }
          } catch (e) {
            console.error('Save button error:', e);
          }
        })();
      }}
    >
      {t('knowledgeConfiguration.save')}
    </ButtonLoading>
  );
}
